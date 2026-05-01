# =============================================================================
# rag_systems.py
# %run this file in any notebook to get all three RAG systems ready:
#   vector_rag(query)   -- vector search  -> LLM answer
#   graph_rag(query)    -- graph traversal -> LLM answer
#   run(query)          -- agentic RAG with episodic memory
# =============================================================================

from __future__ import annotations

import subprocess, sys
subprocess.run([
    sys.executable, "-m", "pip", "install", "-q",
    "langchain-google-genai",
    "langchain-core",
    "langchain-huggingface",
    "networkx",
    "nest_asyncio",
], check=True)

import json
import pathlib
import re as _re
import uuid
from collections import Counter
from datetime import datetime

import networkx as nx
import nest_asyncio
nest_asyncio.apply()

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_huggingface import HuggingFaceEmbeddings

print("Libraries ready.")

# ── Credentials & models ──────────────────────────────────────────────────────

import os
try:
    from google.colab import userdata
    GEMINI_API_KEY = userdata.get('GOOGLE_API_KEY')
    HF_TOKEN       = userdata.get('HF_TOKEN')
    print("Keys loaded from Colab Secrets.")
except Exception:
    GEMINI_API_KEY = os.getenv('GOOGLE_API_KEY', '')
    HF_TOKEN       = os.getenv('HF_TOKEN', '')
    print("Reading from environment variables.")

GEMINI_MODEL = "gemini-3-flash-preview"
EMBED_MODEL  = "BAAI/bge-small-en-v1.5"
GRAPH_FILE   = "incident_knowledge_graph.graphml"
if not pathlib.Path(GRAPH_FILE).exists() and pathlib.Path("incident_knowledge_graph.graphml.xml").exists():
    GRAPH_FILE = "incident_knowledge_graph.graphml.xml"

chat_model = ChatGoogleGenerativeAI(
    model=GEMINI_MODEL,
    temperature=0.1,
    google_api_key=GEMINI_API_KEY,
)
print(f"LLM ready — {GEMINI_MODEL}")

embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL)
print(f"Embeddings ready — {EMBED_MODEL} (local)")

# ── Knowledge graph ───────────────────────────────────────────────────────────

G = nx.read_graphml(GRAPH_FILE)
print(f"\nGraph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
type_counts = Counter(d.get('type', 'unknown') for _, d in G.nodes(data=True))
for t, n in sorted(type_counts.items()):
    print(f"  {t:<20} {n}")

# ── Vector store ──────────────────────────────────────────────────────────────

docs = [
    Document(
        page_content=(
            data.get('summary', '') or data.get('description', '')
            or f"{node} is a {data.get('type', 'entity')}"
        ),
        metadata={'name': node, 'type': data.get('type', 'entity')},
    )
    for node, data in G.nodes(data=True)
]
vector_store = InMemoryVectorStore(embedding=embeddings)
vector_store.add_documents(docs)
print(f"\nVector store ready — {len(docs)} nodes indexed")

# ── Runbooks ──────────────────────────────────────────────────────────────────

_RUNBOOKS = {
    "auth_service":     {"owner": "Identity Team",
                         "steps": ["1. kubectl logs -l app=auth-service",
                                   "2. Verify token validation config",
                                   "3. kubectl rollout undo deploy/auth-service",
                                   "4. Flush session cache",
                                   "5. Page Identity Team on-call"]},
    "api_gateway":      {"owner": "Platform Team",
                         "steps": ["1. Check Gateway error rate in Grafana",
                                   "2. Inspect routing config for recent changes",
                                   "3. kubectl rollout undo deploy/api-gateway",
                                   "4. Page Platform Team on-call"]},
    "database_service": {"owner": "Data Engineering Team",
                         "steps": ["1. Check connection pool utilisation",
                                   "2. Kill long-running queries",
                                   "3. Scale read replicas",
                                   "4. Page Data Engineering on-call"]},
    "cache_service":    {"owner": "Infrastructure Team",
                         "steps": ["1. Check cache hit rate",
                                   "2. Review TTL configuration",
                                   "3. Flush stale session keys",
                                   "4. Page Infrastructure Team on-call"]},
    "default":          {"owner": "Platform Team",
                         "steps": ["1. Identify affected service",
                                   "2. Check recent deployments",
                                   "3. Contact owning team",
                                   "4. Review dependency graph"]},
}

# =============================================================================
# 1. VECTOR RAG
#    Pure similarity search — no graph, no routing.
#    Best for: "what is X?", "describe Y", direct entity questions.
# =============================================================================

_VEC_SYS = (
    "You are an expert SRE analyst.\n\n"
    "Context from the knowledge graph (vector similarity search):\n{context}\n\n"
    "Rules:\n"
    "- Answer ONLY using the context above. Do not add outside knowledge.\n"
    "- If the context does not contain enough information, say: "
    "'I don't have enough information in the knowledge graph to answer this.'\n"
    "- Cite node names where relevant.\n"
    "- Do not show similarity scores."
)
_vec_prompt = ChatPromptTemplate.from_messages([
    ("system", _VEC_SYS), ("human", "{query}")])
_vec_chain = _vec_prompt | chat_model | StrOutputParser()

def vector_rag(query: str) -> str:
    """Vector search -> LLM answer (no graph, no memory)."""
    hits = vector_store.similarity_search_with_score(query, k=5)
    context = '\n'.join(
        f"  {doc.metadata['name']}  (score={score:.3f})\n    {doc.page_content[:150]}"
        for doc, score in hits
    ) if hits else "No relevant nodes found."
    return _vec_chain.invoke({"query": query, "context": context})

print("\nvector_rag() ready.")

# =============================================================================
# 2. GRAPH RAG
#    BFS traversal from seeded nodes — no LLM routing, no memory.
#    Best for: "who owns X?", "what caused Y?", relationship chains.
# =============================================================================

_GRAPH_SYS = (
    "You are an expert SRE analyst.\n\n"
    "Context from the knowledge graph (BFS traversal):\n{context}\n\n"
    "Rules:\n"
    "- Answer ONLY using the context above. Do not add outside knowledge.\n"
    "- If the context does not contain enough information, say: "
    "'I don't have enough information in the knowledge graph to answer this.'\n"
    "- Follow and explain the edge chain (source --[REL]--> target).\n"
    "- Cite node names and relationship types where relevant."
)
_graph_prompt = ChatPromptTemplate.from_messages([
    ("system", _GRAPH_SYS), ("human", "{query}")])
_graph_chain = _graph_prompt | chat_model | StrOutputParser()

def _find_seeds(query: str, k: int = 3) -> list:
    """Name-match first, then vector search fills remaining slots."""
    q_lower = query.lower()
    direct  = [n for n in G.nodes if n.lower() in q_lower]
    hits    = vector_store.similarity_search(query, k=k)
    vector  = [h.metadata['name'] for h in hits if h.metadata['name'] in G.nodes]
    seen, seeds = set(), []
    for n in direct + vector:
        if n not in seen:
            seen.add(n); seeds.append(n)
    return seeds[:k]

def _bfs_context(query: str) -> str:
    seed_nodes = _find_seeds(query, k=3)
    if not seed_nodes:
        return "No relevant graph nodes found."
    lines    = [f"Seed nodes: {', '.join(seed_nodes)}"]
    visited  = set(seed_nodes)
    frontier = list(seed_nodes)

    # Edges between seed nodes themselves
    for u in seed_nodes:
        for v in seed_nodes:
            if u != v:
                edge = G.get_edge_data(u, v)
                if edge:
                    lines.append(
                        f"{u} --[{edge.get('rel','?')}]--> {v} "
                        f"[{G.nodes[v].get('type','entity')}]"
                    )
    # BFS 2 hops
    for hop in range(2):
        nxt    = []
        indent = '  ' * (hop + 1)
        for node in frontier:
            for nb in G.successors(node):
                if nb not in visited:
                    visited.add(nb); nxt.append(nb)
                    rel = (G.get_edge_data(node, nb) or {}).get('rel', '?')
                    lines.append(
                        f"{indent}{node} --[{rel}]--> {nb} "
                        f"[{G.nodes[nb].get('type','entity')}]"
                    )
            for nb in G.predecessors(node):
                if nb not in visited:
                    visited.add(nb); nxt.append(nb)
                    rel = (G.get_edge_data(nb, node) or {}).get('rel', '?')
                    lines.append(
                        f"{indent}{nb} --[{rel}]--> {node} "
                        f"[{G.nodes[node].get('type','entity')}]"
                    )
        frontier = nxt

    return '\n'.join(lines) if len(lines) > 1 else "No connected nodes found."

def graph_rag(query: str) -> str:
    """BFS graph traversal -> LLM answer (no agent routing, no memory)."""
    context = _bfs_context(query)
    return _graph_chain.invoke({"query": query, "context": context})

print("graph_rag() ready.")

# =============================================================================
# 3. AGENTIC RAG WITH MEMORY
#    RECALL -> THINK -> EXECUTE -> ANSWER -> LOG
#    Agent picks the right skill; episodic memory persists between runs.
# =============================================================================

# ── Skill registry ────────────────────────────────────────────────────────────

from dataclasses import dataclass

@dataclass
class SkillDef:
    name:         str
    description:  str   # injected into THINK — LLM reads this to pick a skill
    instructions: str   # injected into ANSWER — guides synthesis after execution

SKILLS = [
    SkillDef(
        name="vector-lookup",
        description=(
            "Semantic search over the knowledge graph. "
            "Use for direct entity questions: what is X, describe Y, tell me about Z."
        ),
        instructions=(
            "The tool returns the top-5 graph nodes most similar to the query. "
            "Cite node names. "
            "Best for: service descriptions, team information, incident summaries."
        ),
    ),
    SkillDef(
        name="graph-traversal",
        description=(
            "Multi-hop graph traversal. "
            "Use for relational questions: who owns X, what caused Y, what depends on Z, "
            "which services break if X goes offline, what happened downstream."
        ),
        instructions=(
            "The tool returns a BFS traversal showing each hop as "
            "source --[REL]--> target [type]. "
            "Follow the edge chain and explain the relationship path. "
            "Best for: ownership chains, causation paths, dependency trees."
        ),
    ),
    SkillDef(
        name="runbook-lookup",
        description=(
            "Incident remediation runbook. "
            "Use for any how-to-fix or how-to-resolve question: "
            "fix X, resolve X, remediate X, rollback X, steps to handle X, "
            "connection pool, cache, gateway, auth, database issues."
        ),
        instructions=(
            "The tool returns the owner team and ordered remediation steps. "
            "Present the steps clearly and name the on-call team. "
            "Best for: active incidents requiring immediate action."
        ),
    ),
]

SKILLS_MAP   = {s.name: s for s in SKILLS}
_SKILL_LIST  = '\n'.join(f'  - {s.name}: {s.description}' for s in SKILLS)
_VALID_NAMES = ', '.join(s.name for s in SKILLS)

# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def vector_lookup(query: str) -> str:
    """Semantic search over the knowledge graph. Use for direct entity questions: what is X, describe Y, tell me about Z."""
    hits = vector_store.similarity_search_with_score(query, k=5)
    if not hits:
        return 'No relevant nodes found.'
    return '\n'.join(
        f"  {doc.metadata['name']}  (score={score:.3f})\n    {doc.page_content[:120]}"
        for doc, score in hits
    )

@tool
def graph_traversal(query: str) -> str:
    """Multi-hop graph traversal. Use for relational questions: who owns X, what caused Y, what depends on Z."""
    return _bfs_context(query)

@tool
def runbook_lookup(query: str) -> str:
    """Incident remediation runbook. Use for how-to-fix questions: steps to resolve, rollback procedure, on-call owner."""
    q = query.lower()
    if   'auth' in q or 'token' in q:      service = 'auth_service'
    elif 'gateway' in q or 'routing' in q: service = 'api_gateway'
    elif 'database' in q or 'pool' in q:   service = 'database_service'
    elif 'cache' in q or 'session' in q:   service = 'cache_service'
    else:                                   service = 'default'
    rb = _RUNBOOKS[service]
    return f"Runbook [{service}]  owner: {rb['owner']}\n" + '\n'.join(rb['steps'])

_TOOLS = {
    'vector-lookup':   vector_lookup,
    'graph-traversal': graph_traversal,
    'runbook-lookup':  runbook_lookup,
}

# ── Chains & parsing ──────────────────────────────────────────────────────────

_THINK_MEM = (
    "You are an agent selecting the right skill to investigate the user's query.\n\n"
    "Past similar incidents (avoid repeating steps already tried):\n{memory_context}\n\n"
    "Observations this session:\n{observations}\n\n"
    "Available skills:\n{skill_list}\n\n"
    "Valid skill names: {valid_names}\n"
    "You MUST pick one skill from the list above. Never use none.\n"
    "If unsure, default to vector-lookup.\n\n"
    "Reply with ONLY a JSON object — no markdown, no code fences:\n"
    "{{\"thought\": \"what you still need to find out\", \"skill\": \"skill-name\"}}"
)
_think_mem_prompt = ChatPromptTemplate.from_messages([
    ("system", _THINK_MEM), ("human", "Query: {query}")])
_think_mem_chain = _think_mem_prompt | chat_model | StrOutputParser()

_ANSWER_MEM = (
    "You are an expert SRE analyst.\n\n"
    "Skill instructions:\n{instructions}\n\n"
    "Past episode context (use for questions about previous incidents):\n{memory_context}\n\n"
    "Context retrieved by the skill this session:\n{context}\n\n"
    "Rules:\n"
    "- Answer ONLY using the context above. Do not add outside knowledge.\n"
    "- If the context does not contain enough information, say: "
    "'I don't have enough information in the knowledge graph to answer this.'\n"
    "- Cite node names and relationship types where relevant.\n"
    "- Do not show similarity scores."
)
_answer_mem_prompt = ChatPromptTemplate.from_messages([
    ("system", _ANSWER_MEM), ("human", "{query}")])
_answer_mem_chain = _answer_mem_prompt | chat_model | StrOutputParser()

def _parse_think(raw: str):
    cleaned = _re.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
    try:
        data    = json.loads(cleaned)
        thought = str(data.get('thought', '')).strip()
        skill   = str(data.get('skill',   '')).strip().lower()
        return thought, skill
    except (json.JSONDecodeError, ValueError):
        thought = skill = ''
        for line in raw.splitlines():
            clean = line.replace('**', '').replace('*', '').strip()
            if clean.upper().startswith('THOUGHT:'):
                thought = clean.split(':', 1)[1].strip()
            elif clean.upper().startswith('SKILL:'):
                skill = clean.split(':', 1)[1].strip().lower()
        return thought, skill

def _match_skill(raw_skill: str):
    normalised = raw_skill.replace('-', ' ').replace('_', ' ')
    for name in _TOOLS:
        if name in raw_skill or name.replace('-', ' ') in normalised:
            return name
    return None

def _fallback_skill(query: str) -> str:
    q = query.lower()
    # Runbook first — fix/remediation questions take priority
    if any(w in q for w in ['how to', 'fix', 'resolve', 'rollback', 'steps',
                             'runbook', 'remediat', 'remediation']):
        return 'runbook-lookup'
    # Graph-traversal — relationship + membership questions
    if any(w in q for w in ['who owns', 'who is', 'who are', 'members', 'member of',
                             'depends', 'caused', 'responsible', 'owner', 'team',
                             'break', 'breaks', 'offline', 'goes down', 'affect', 'impact', 'triggered',
                             'what happens', 'downstream', 'upstream', 'which service',
                             'authored', 'incident', 'before', 'dealt with',
                             'resolved', 'resolution', 'previous', 'investigation',
                             'how was', 'what happened', 'inc-']):
        return 'graph-traversal'
    return 'vector-lookup'

# ── Episodic memory ───────────────────────────────────────────────────────────

MEMORY_FILE = pathlib.Path('episodic_memory.graphml')

class EpisodicMemory:
    def __init__(self):
        self._ep = None
        if MEMORY_FILE.exists():
            self._G = nx.read_graphml(str(MEMORY_FILE))
            print(f'Memory loaded — {self._G.number_of_nodes()} past episodes')
        else:
            self._G = nx.DiGraph()
            print('Memory initialised (empty)')

    def start_episode(self, goal: str):
        self._ep = f"ep_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        self._G.add_node(self._ep, ts=datetime.now().isoformat(),
                         goal=goal, steps='[]', result='')
        prior = max(
            (n for n in self._G.nodes if n != self._ep),
            key=lambda n: self._G.nodes[n].get('ts', ''), default=None
        )
        if prior:
            self._G.add_edge(self._ep, prior, rel='PRECEDED_BY')

    def log_step(self, decision: str, tool_name: str, observation: str):
        steps = json.loads(self._G.nodes[self._ep].get('steps', '[]'))
        steps.append({'decision': decision, 'tool': tool_name, 'obs': observation[:200]})
        self._G.nodes[self._ep]['steps'] = json.dumps(steps)

    def end_episode(self, result: str):
        self._G.nodes[self._ep]['result'] = result[:400]
        nx.write_graphml(self._G, str(MEMORY_FILE))
        print(f'  [MEMORY] Saved — {self._G.number_of_nodes()} total episodes')

    def retrieve(self, query: str, k: int = 3) -> str:
        past = [(n, d) for n, d in self._G.nodes(data=True)
                if n != self._ep and d.get('result')]
        if not past:
            return 'No past episodes.'
        now = datetime.now()
        q_w = {w for w in query.lower().split() if len(w) > 3}

        def score(d):
            g_w = {w for w in d.get('goal', '').lower().split() if len(w) > 3}
            sim = len(q_w & g_w) / max(len(q_w | g_w), 1) if q_w else 1.0
            try:
                age = (now - datetime.fromisoformat(d.get('ts', now.isoformat()))).days
            except ValueError:
                age = 0
            return 0.6 * sim + 0.4 * (2 ** (-age / 7))

        ranked = sorted(past, key=lambda x: score(x[1]), reverse=True)[:k]
        lines  = []
        for _, d in ranked:
            chain = ' -> '.join(s['tool'] for s in json.loads(d.get('steps', '[]'))) or 'none'
            lines.append(
                f"[{d.get('ts','')[:19]}] Goal: {d.get('goal','')}\n"
                f"  Tools: {chain}\n  Result: {d.get('result','')[:150]}"
            )
        return '\n\n'.join(lines)

memory = EpisodicMemory()

# ── Pre-seed memory with past episodes ───────────────────────────────────────

def _seed_memory():
    """
    Inject realistic past episodes so memory is non-empty from the first run.
    Skipped if the memory file already exists (i.e. real runs have been logged).
    Each episode mimics what run() would have stored after a real investigation.
    """
    # Always upsert seed episodes by ID so they stay current across re-runs.
    # Only skip an episode if it's already present with the correct goal.
    existing_goals = {
        d.get("goal", ""): n
        for n, d in memory._G.nodes(data=True)
    }

    _SEED_EPISODES = [
        {
            "id":    "ep_seed_001",
            "ts":    "2026-04-21T09:14:32",
            "goal":  "Who authored the deployment that triggered INC-003?",
            "steps": json.dumps([
                {
                    "decision": "Multi-hop question — trace from INC-003 back to the deployment and then to its author.",
                    "tool":     "graph-traversal",
                    "obs":      (
                        "Seed nodes: INC-003, API Gateway v3.2.1\n"
                        "API Gateway v3.2.1 --[TRIGGERED]--> INC-003 [incident]\n"
                        "API Gateway v3.2.1 --[AUTHORED_BY]--> David [member]\n"
                        "API Gateway v3.2.1 --[DEPLOYED_BY]--> Platform Team [team]\n"
                        "Auth Service v2.1 --[TRIGGERED]--> INC-003 [incident]\n"
                        "Auth Service v2.1 --[AUTHORED_BY]--> Alice [member]"
                    ),
                }
            ]),
            "result": (
                "INC-003 was triggered by two deployments: "
                "API Gateway v3.2.1 authored by David (Platform Team), "
                "and Auth Service v2.1 authored by Alice (Identity Team)."
            ),
        },
        {
            "id":    "ep_seed_004",
            "ts":    "2026-04-21T11:30:00",
            "goal":  "How was INC-003 resolved?",
            "steps": json.dumps([
                {
                    "decision": "Resolution question — check graph for RESOLVED_BY edges and past runbook steps.",
                    "tool":     "graph-traversal",
                    "obs":      (
                        "Seed nodes: INC-003\n"
                        "INC-003 --[RESOLVED_BY]--> Omar [member]\n"
                        "INC-003 --[RESOLVED_BY]--> Sara [member]\n"
                        "API Gateway v3.2.1 --[TRIGGERED]--> INC-003 [incident]\n"
                        "API Gateway v3.2.1 --[DEPLOYED_BY]--> Platform Team [team]"
                    ),
                }
            ]),
            "result": (
                "INC-003 was resolved by rolling back API Gateway v3.2.1 to the previous stable version. "
                "Omar and Sara from the Platform Team executed the rollback and confirmed service recovery. "
                "Root cause: the API Gateway v3.2.1 deployment introduced a misconfiguration "
                "that destabilised token validation in Auth Service."
            ),
        },
        {
            "id":    "ep_seed_002",
            "ts":    "2026-04-22T14:03:11",
            "goal":  "How do I fix the API Gateway after a bad deployment?",
            "steps": json.dumps([
                {
                    "decision": "User wants remediation steps — runbook-lookup is the right skill.",
                    "tool":     "runbook-lookup",
                    "obs":      (
                        "Runbook [api_gateway]  owner: Platform Team\n"
                        "1. Check Gateway error rate in Grafana\n"
                        "2. Inspect routing config for recent changes\n"
                        "3. kubectl rollout undo deploy/api-gateway\n"
                        "4. Page Platform Team on-call"
                    ),
                }
            ]),
            "result": (
                "To fix the API Gateway after a bad deployment: "
                "(1) Check the Gateway error rate in Grafana, "
                "(2) Inspect routing config for recent changes, "
                "(3) Run: kubectl rollout undo deploy/api-gateway, "
                "(4) Page the Platform Team on-call. Owner: Platform Team."
            ),
        },
        {
            "id":    "ep_seed_003",
            "ts":    "2026-04-23T08:47:55",
            "goal":  "Which services were affected when INC-003 occurred?",
            "steps": json.dumps([
                {
                    "decision": "Need to find what INC-003 affected — graph traversal from the incident node.",
                    "tool":     "graph-traversal",
                    "obs":      (
                        "Seed nodes: INC-003\n"
                        "INC-003 --[AFFECTS]--> Auth Service [service]\n"
                        "INC-003 --[AFFECTS]--> API Gateway [service]\n"
                        "Auth Service --[DEPENDS_ON]--> Cache Service [service]\n"
                        "API Gateway --[DEPENDS_ON]--> Auth Service [service]"
                    ),
                }
            ]),
            "result": (
                "INC-003 affected both API Gateway and Auth Service directly. "
                "Because API Gateway depends on Auth Service, and Auth Service depends on Cache Service, "
                "the downstream blast radius included the Cache Service as well. "
                "The root cause was the Auth Service v2.1 deployment which destabilised token validation."
            ),
        },
    ]

    added = 0
    for ep in _SEED_EPISODES:
        # Upsert: always write seed episodes so updates to goals/results take effect
        memory._G.add_node(
            ep["id"],
            ts=ep["ts"],
            goal=ep["goal"],
            steps=ep["steps"],
            result=ep["result"],
        )
        added += 1

    # Chain chronologically: each episode PRECEDED_BY the one before it
    ordered = sorted(_SEED_EPISODES, key=lambda e: e["ts"])
    for i in range(len(ordered) - 1):
        memory._G.add_edge(ordered[i + 1]["id"], ordered[i]["id"], rel="PRECEDED_BY")

    nx.write_graphml(memory._G, str(MEMORY_FILE))
    print(f"  [MEMORY] Upserted {added} seed episodes → {MEMORY_FILE.name}")

_seed_memory()

# ── run() — agentic RAG with memory ──────────────────────────────────────────

def run(query: str) -> str:
    """RECALL -> THINK -> EXECUTE -> ANSWER -> LOG."""
    div = '=' * 64
    print(f'\n{div}\nQUERY: {query}\n{div}')

    # 1. RECALL
    memory.start_episode(query)
    mem_ctx = memory.retrieve(query)
    first   = mem_ctx.splitlines()[0] if mem_ctx != 'No past episodes.' else 'none'
    print(f'\nRECALL : {first}')

    # 2. THINK
    raw = _think_mem_chain.invoke({
        'valid_names':    _VALID_NAMES,
        'skill_list':     _SKILL_LIST,
        'memory_context': mem_ctx,
        'observations':   'None yet.',
        'query':          query,
    })
    thought, raw_skill = _parse_think(raw)
    matched = _match_skill(raw_skill)
    if matched is None:
        matched = _fallback_skill(query)
        thought = thought or f'[fallback] {matched}'

    print(f'THINK  : {thought}')
    print(f'SKILL  : {matched}')

    # 3. EXECUTE
    skill_def    = SKILLS_MAP[matched]
    instructions = skill_def.instructions
    tool_output  = _TOOLS[matched].invoke(query)
    print(f'\n-- {matched} output --\n{tool_output}\n---')
    memory.log_step(thought, matched, tool_output)

    # 4. ANSWER
    answer = _answer_mem_chain.invoke({
        'instructions':   instructions,
        'memory_context': mem_ctx,
        'context':        tool_output,
        'query':          query,
    })

    # 5. LOG
    memory.end_episode(answer)
    print(f'\nANSWER :\n{answer}\n{div}')
    return answer

# =============================================================================
print("\n" + "=" * 64)
print("All three RAG systems ready:")
print("  vector_rag(query)  — vector search -> LLM answer")
print("  graph_rag(query)   — BFS traversal -> LLM answer")
print("  run(query)         — agentic RAG with episodic memory")
print("=" * 64)
