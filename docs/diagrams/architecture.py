"""Generate docs/images/architecture.svg for the README.

Usage (from the repo root):
    python3 docs/diagrams/architecture.py docs/images/architecture.svg

To refresh the PNG, render the SVG at 2400px wide with any SVG renderer,
e.g. `npx @resvg/resvg-js-cli --fit-width 2400 docs/images/architecture.svg docs/images/architecture.png`.
"""
import html, sys

W, H = 1600, 1480
SANS = "Helvetica Neue, Helvetica, Arial, sans-serif"
MONO = "Menlo, SFMono-Regular, Consolas, monospace"
out = []
e = lambda s: html.escape(s, quote=True)

def rect(x, y, w, h, fill, stroke, rx=12, sw=1.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

def text(x, y, s, size=13, color="#0f172a", weight="normal", anchor="start", mono=False, italic=False):
    st = ' font-style="italic"' if italic else ""
    out.append(f'<text x="{x}" y="{y}" font-family="{MONO if mono else SANS}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}"{st}>{e(s)}</text>')

def tw(s, size, mono=False):
    return len(s) * size * (0.602 if mono else 0.54)

def line(pts, color="#475569", sw=1.8, arrow=True, dash=None):
    p = " ".join(f"{a},{b}" for a, b in pts)
    m = ' marker-end="url(#ah)"' if arrow else ""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{sw}"{m}{d}/>')

def badge(x, y, n, color):
    out.append(f'<circle cx="{x}" cy="{y}" r="13" fill="{color}"/>')
    text(x, y + 5, str(n), 14, "#ffffff", "bold", "middle")

def chip(x, y, s, stroke, size=13, h=30, fill="#ffffff", color="#0f172a", mono=True):
    w = tw(s, size, mono) + 22
    rect(x, y, w, h, fill, stroke, rx=7, sw=1.2)
    text(x + w / 2, y + h / 2 + size * 0.36, s, size, color, "normal", "middle", mono)
    return x + w

def section(x, y, w, h, n, title, sub, fill, stroke, dark):
    rect(x, y, w, h, fill, stroke, rx=16, sw=2)
    badge(x + 30, y + 30, n, stroke)
    text(x + 52, y + 36, title, 19, dark, "bold")
    if sub:
        text(x + 52, y + 58, sub, 12.5, dark, mono=True)

out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
out.append('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#475569"/></marker></defs>')
rect(0, 0, W, H, "#ffffff", "#e2e8f0", rx=0, sw=0)

# ---- title ----------------------------------------------------------------
text(40, 56, "Travel Policy RAG — System Architecture", 30, "#0f172a", "bold")
text(40, 86, "Grounded answers with clause-level citations over a corporate travel policy.  Retrieval maths, cost accounting and tenant isolation live in PostgreSQL; the API stays thin.", 15, "#475569")

# ---- 1. browser -------------------------------------------------------------
BX, BY, BW, BH = 40, 112, 1520, 132
section(BX, BY, BW, BH, 1, "Browser — React 19 · Vite 8 · Tailwind 4", None, "#eff6ff", "#3b82f6", "#1e3a8a")
text(BX + BW - 24, BY + 36, "web/  ·  dev server :5173  ·  proxies /api → http://127.0.0.1:8000", 13, "#1e40af", anchor="end", mono=True)
cx = BX + 24
for s in ["AnswerPanel", "SourcesPanel", "CostMeter", "ProvenancePanel", "DocumentPanel", "lib/useAskStream.ts — SSE over fetch + ReadableStream"]:
    cx = chip(cx, BY + 56, s, "#93c5fd") + 12
text(BX + 24, BY + 112, "Ask a question → watch the pipeline stages → read the streamed, cited answer → open a citation's source clause → inspect the run's cost and full provenance.", 13, "#334155")

# ---- arrows browser <-> api -------------------------------------------------
line([(640, BY + BH + 2), (640, 302)])
text(652, BY + BH + 30, "POST /api/ask {question}   ·   GET /api/*", 12.5, "#334155", mono=True)
line([(560, 300), (560, BY + BH + 4)])
text(548, BY + BH + 30, "text/event-stream: stage · token · citations · cost · done · error", 12.5, "#334155", anchor="end", mono=True)

# ---- 2. api -----------------------------------------------------------------
AX, AY, AW, AH = 40, 304, 1520, 176
section(AX, AY, AW, AH, 2, "FastAPI — backend/app/main.py", None, "#f5f3ff", "#8b5cf6", "#4c1d95")
text(AX + AW - 24, AY + 36, "thin by design:  validate → set tenant GUC → drive the pipeline → serialise events", 13.5, "#5b21b6", anchor="end", italic=True)
cx = AX + 24
for s in ["GET /api/health", "GET /api/config", "GET /api/documents", "POST /api/documents/estimate", "POST /api/documents/ingest"]:
    cx = chip(cx, AY + 54, s, "#c4b5fd") + 12
cx = AX + 24
for s in ["POST /api/ask  (SSE)", "GET /api/runs", "GET /api/runs/{run_id}/provenance", "GET /api/costs/summary"]:
    cx = chip(cx, AY + 94, s, "#c4b5fd", fill="#ede9fe" if "ask" in s else "#ffffff") + 12
text(AX + 24, AY + 154, "startup: register config/*.yaml → pin config bundle   ·   api/deps.py: current_principal → (tenant_id, user_id)   ·   db/pool.py: asyncpg pool connecting as rag_app (NOSUPERUSER, NOBYPASSRLS)", 12.5, "#4c1d95")

# ---- column geometry --------------------------------------------------------
CY, CH = 540, 560          # row C top / height
GX, GW = 40, 600           # graph
PX, PW = 690, 300          # providers
IX, IW = 1040, 520         # ingestion + config

# arrows api -> graph / ingestion
line([(300, AY + AH + 2), (300, CY - 2)])
text(312, AY + AH + 36, "POST /api/ask drives the pipeline", 12.5, "#334155", mono=True)
line([(1300, AY + AH + 2), (1300, CY - 2)])
text(1288, AY + AH + 36, "POST /api/documents/estimate · ingest", 12.5, "#334155", anchor="end", mono=True)

# ---- 3. graph ---------------------------------------------------------------
section(GX, CY, GW, CH, 3, "Query pipeline — LangGraph", "backend/app/graph · driven step by step by /api/ask", "#f0fdfa", "#14b8a6", "#134e4a")
SX, SW, NH, STEP = GX + 20, 280, 40, 50
y0 = CY + 84
nodes = [
    ("guard", "trim · reject empty or > 2000 chars"),
    ("cache_lookup", "exact hash, then semantic ≥ 0.97"),
    ("route", "intent + metadata filter · Opus 5 (low)"),
    ("translate", "multi-query + step-back · Opus 5 (low)"),
    ("retrieve", "SQL: vector + lexical → RRF → MMR → expand"),
    ("rerank", "listwise relevance · Opus 5 (high)"),
    ("generate", "cited answer, token-streamed · Opus 5 (high)"),
    ("verify", "regex citation check · no LLM call"),
]
ys = {}
for i, (n, d) in enumerate(nodes):
    y = y0 + i * STEP
    ys[n] = y + NH / 2
    fill = "#ffffff" if n not in ("rerank", "generate") else "#ecfeff"
    rect(SX, y, SW, NH, fill, "#5eead4", rx=8, sw=1.3)
    text(SX + 12, y + 17, n, 14, "#134e4a", "bold", mono=True)
    text(SX + 12, y + 33, d, 11.5, "#334155")
    if i:
        line([(SX + SW / 2, y - STEP + NH + 1), (SX + SW / 2, y - 1)], "#14b8a6", 1.6)
endy = y0 + len(nodes) * STEP + 4
line([(SX + SW / 2, endy - STEP + NH - 3), (SX + SW / 2, endy - 1)], "#14b8a6", 1.6)
rect(SX + SW / 2 - 36, endy, 72, 24, "#134e4a", "#134e4a", rx=12)
text(SX + SW / 2, endy + 17, "END", 12, "#ffffff", "bold", "middle", mono=True)

EX, EW = GX + GW - 20 - 150, 150
def exit_box(yc, h, title, sub, col="#fef2f2", st="#f87171", dk="#7f1d1d"):
    rect(EX, yc - h / 2, EW, h, col, st, rx=8, sw=1.3)
    text(EX + EW / 2, yc - 2, title, 13.5, dk, "bold", "middle", mono=True)
    text(EX + EW / 2, yc + 14, sub, 11, dk, anchor="middle")
exit_box(ys["cache_lookup"], 40, "cached answer", "→ END", "#f0fdf4", "#4ade80", "#14532d")
line([(SX + SW, ys["cache_lookup"]), (EX - 2, ys["cache_lookup"])], "#16a34a", 1.5)
text(SX + SW + 27, ys["cache_lookup"] - 6, "hit", 11, "#166534", mono=True)
exit_box(ys["route"], 40, "clarify", "ask, don't guess → END", "#fffbeb", "#fbbf24", "#78350f")
line([(SX + SW, ys["route"] - 6), (EX - 2, ys["route"] - 6)], "#d97706", 1.5)
text(SX + SW + 27, ys["route"] - 12, "underspecified", 10.5, "#92400e", mono=True)
ry0, ry1 = ys["retrieve"] - 30, ys["rerank"] + 30
rect(EX, ry0, EW, ry1 - ry0, "#fef2f2", "#f87171", rx=8, sw=1.3)
text(EX + EW / 2, (ry0 + ry1) / 2 - 6, "refuse", 13.5, "#7f1d1d", "bold", "middle", mono=True)
text(EX + EW / 2, (ry0 + ry1) / 2 + 10, "honest “not covered”", 11, "#7f1d1d", anchor="middle")
text(EX + EW / 2, (ry0 + ry1) / 2 + 25, "→ END", 11, "#7f1d1d", anchor="middle")
bx = SX + SW + 22
line([(SX + SW, ys["route"] + 8), (bx, ys["route"] + 8), (bx, ry0 + 14), (EX - 2, ry0 + 14)], "#dc2626", 1.5)
text(bx + 5, ys["translate"] + 4, "chitchat", 10.5, "#991b1b", mono=True)
line([(SX + SW, ys["retrieve"] + 6), (EX - 2, ys["retrieve"] + 6)], "#dc2626", 1.5)
text(bx + 5, ys["retrieve"] + 1, "0 candidates", 10.5, "#991b1b", mono=True)
line([(SX + SW, ys["rerank"]), (EX - 2, ys["rerank"])], "#dc2626", 1.5)
text(bx + 5, ys["rerank"] - 5, "none ≥ floor", 10.5, "#991b1b", mono=True)
text(GX + 20, CY + CH - 16, "out_of_scope still retrieves: citing the section that points elsewhere beats a bare “no”.", 11.5, "#115e59", italic=True)

# graph -> providers
line([(GX + GW + 2, CY + 250), (PX - 2, CY + 250)])
text((GX + GW + PX) / 2, CY + 242, "roles", 11, "#334155", anchor="middle", mono=True)

# ---- 4. providers -----------------------------------------------------------
section(PX, CY, PW, CH, 4, "Model providers", "backend/app/providers", "#fdf2f8", "#ec4899", "#831843")
rect(PX + 16, CY + 80, PW - 32, 200, "#ffffff", "#f9a8d4", rx=10, sw=1.2)
text(PX + 30, CY + 106, "Anthropic", 15.5, "#831843", "bold")
text(PX + 30, CY + 128, "claude-opus-5", 13.5, "#0f172a", "bold", mono=True)
for i, s in enumerate(["routing · query_translation  (low)", "rerank · generation  (high)", "contextualisation  (ingest, low)", "adaptive thinking · prompt caching", "token streaming for generation"]):
    text(PX + 30, CY + 154 + i * 22, "• " + s, 12, "#334155")
rect(PX + 16, CY + 296, PW - 32, 170, "#ffffff", "#f9a8d4", rx=10, sw=1.2)
text(PX + 30, CY + 322, "Google Gemini", 15.5, "#831843", "bold")
text(PX + 30, CY + 344, "gemini-embedding-001", 13.5, "#0f172a", "bold", mono=True)
for i, s in enumerate(["1536-d (MRL-truncated from 3072,", "   re-normalised to unit length)", "RETRIEVAL_DOCUMENT vs _QUERY", "target: gemini-3.8-flash for", "   routing · translation · context"]):
    text(PX + 30, CY + 368 + i * 20, ("• " if not s.startswith("   ") else "  ") + s.strip(), 12, "#334155")
text(PX + 20, CY + 490, "role → model resolved from config/models.yaml", 11.5, "#831843", italic=True)
text(PX + 20, CY + 508, "each model step → run_steps + cost_ledger", 11.5, "#831843", italic=True)

# ingestion -> providers
line([(IX - 2, CY + 150), (PX + PW + 2, CY + 150)])
text((PX + PW + IX) / 2, CY + 142, "calls", 11, "#334155", anchor="middle", mono=True)

# ---- 5. ingestion -----------------------------------------------------------
IH = 300
section(IX, CY, IW, IH, 5, "Ingestion pipeline", "backend/app/ingest · scripts/ingest.py", "#fffbeb", "#f59e0b", "#78350f")
steps = [
    ("parse + chunk", "structure-aware; tables stay whole"),
    ("estimate", "price recorded before any spend · no keys needed"),
    ("contextualise", "every chunk · Opus 5, document in cached prefix"),
    ("replace chunks", "content-addressed by sha256"),
    ("reuse", "copy forward embeddings of unchanged chunks"),
    ("embed", "only new chunks · gemini-embedding-001 → vector(1536)"),
]
for i, (a, b) in enumerate(steps):
    y = CY + 80 + i * 34
    rect(IX + 20, y, 26, 26, "#f59e0b", "#f59e0b", rx=13)
    text(IX + 33, y + 18, str(i + 1), 13, "#ffffff", "bold", "middle")
    text(IX + 56, y + 18, a, 13.5, "#78350f", "bold", mono=True)
    text(IX + 190, y + 18, b, 12.5, "#334155")

# ---- 6. config ----------------------------------------------------------------
KY, KH = CY + IH + 20, CH - IH - 20
section(IX, KY, IW, KH, 6, "Versioned configuration", "config/*.yaml → backend/app/config/registry.py", "#f8fafc", "#64748b", "#1e293b")
files = ["models.yaml", "costs.yaml", "retrieval.yaml", "chunking.yaml", "prompts/*.yaml  (×5)"]
rect(IX + 20, KY + 74, 196, 112, "#ffffff", "#cbd5e1", rx=8, sw=1.2)
for i, f in enumerate(files):
    text(IX + 34, KY + 96 + i * 20, f, 12, "#1e293b", mono=True)
line([(IX + 218, KY + 130), (IX + 244, KY + 130)], "#64748b", 1.5)
flow = ["canonical JSON  (sorted keys)", "sha256 per file → config_file_versions", "combined hash → config_bundles", "every run records its bundle"]
for i, f in enumerate(flow):
    y = KY + 76 + i * 28
    rect(IX + 250, y, 250, 24, "#f1f5f9", "#94a3b8", rx=6, sw=1.1)
    text(IX + 262, y + 16.5, f, 11.5, "#1e293b")
text(IX + 20, KY + 208, "Code asks for a role, never a model — switching models is a YAML edit.", 12, "#334155")
text(IX + 20, KY + 226, "GET /api/runs/{id}/provenance → prompt versions, models and YAML of any run.", 12, "#334155")

# ---- arrows row C -> DB -----------------------------------------------------
DY = CY + CH + 60
line([(300, CY + CH + 2), (300, DY - 2)])
text(312, CY + CH + 36, "fn_cache_lookup · fn_hybrid_search · run_steps · cost_ledger", 12.5, "#334155", mono=True)
line([(1300, CY + CH + 2), (1300, DY - 2)])
text(1288, CY + CH + 36, "chunks · chunk_embeddings · config_file_versions", 12.5, "#334155", anchor="end", mono=True)

# ---- 7. database --------------------------------------------------------------
DH = H - DY - 30
section(40, DY, 1520, DH, 7, "PostgreSQL 16 + pgvector 0.8.6", None, "#eef2ff", "#6366f1", "#312e81")
text(1536, DY + 36, "migrations/001–007  ·  scripts/migrate.sh", 13, "#3730a3", anchor="end", mono=True)
text(420, DY + 36, "the database owns retrieval maths, cost accounting and tenant isolation", 14, "#3730a3", italic=True)
cards = [
    ("Tenancy · RLS", ["tenants", "users", "RLS policies keyed on", "  the tenant GUC", "app role rag_app:", "  NOSUPERUSER NOBYPASSRLS"]),
    ("Config · provenance", ["config_file_versions", "config_bundles", "prompt_versions", "model_versions", "role_assignments", "model_prices"]),
    ("Documents · chunks", ["documents", "document_versions", "chunks", "  tsvector GIN index", "chunk_embeddings", "  vector(1536) · HNSW"]),
    ("Retrieval functions", ["fn_vector_search", "fn_lexical_search", "fn_hybrid_search (RRF)", "fn_mmr_diversify", "fn_expand_context"]),
    ("Runs · cost · budget", ["query_runs · ingestion_runs", "run_steps", "retrieval_results", "cost_ledger", "  cost_usd = generated col", "tenant_budgets + trigger"]),
    ("Views · answer cache", ["v_run_provenance", "v_cost_per_query", "v_cost_by_node / _by_user", "v_run_cost_breakdown", "answer_cache", "fn_cache_lookup"]),
]
cw, gap = (1520 - 40 - 5 * 14) / 6, 14
for i, (t, rows) in enumerate(cards):
    x = 60 + i * (cw + gap)
    rect(x, DY + 56, cw, DH - 96, "#ffffff", "#a5b4fc", rx=10, sw=1.2)
    text(x + 14, DY + 80, t, 14, "#312e81", "bold")
    for j, r in enumerate(rows):
        ind = r.startswith("  ")
        text(x + (26 if ind else 14), DY + 104 + j * 19, r.strip(), 11.5 if ind else 12.5, "#64748b" if ind else "#1e293b", mono=not ind)
text(60, DY + DH - 16, "/api/runs/{id}/provenance and /api/costs/summary read these views; ranking maths, cost arithmetic and aggregation stay in SQL.", 12.5, "#3730a3")

out.append("</svg>")
open(sys.argv[1], "w").write("\n".join(out))
print("ok", sys.argv[1])
