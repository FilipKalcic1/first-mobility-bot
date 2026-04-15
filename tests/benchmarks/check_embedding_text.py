"""Quick check of V6.0 embedding text for a few tools."""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.faiss_vector_store import FAISSVectorStore

doc_path = project_root / "config" / "tool_documentation.json"
with open(doc_path, "r", encoding="utf-8") as f:
    docs = json.load(f)

store = FAISSVectorStore()
# Check a few representative tools
sample_tools = [
    "delete_Cases_id",
    "delete_Vehicles_id",
    "delete_Equipment_id",
    "get_Lookup",
    "get_Vehicles",
    "get_PeriodicActivityTypes",
    "post_Partners_id_documents",
]

for tool_id in sample_tools:
    if tool_id in docs:
        text = store._build_embedding_text(tool_id, docs[tool_id])
        print(f"\n{'='*60}")
        print(f"TOOL: {tool_id}")
        print(f"TEXT ({len(text)} chars):")
        print(text[:500])
        print("..." if len(text) > 500 else "")
