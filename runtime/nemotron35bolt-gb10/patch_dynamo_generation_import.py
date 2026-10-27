"""Keep Dynamo's unused multi-process embedding imports out of Bolt startup."""

import ast
import hashlib
from importlib.metadata import version
from pathlib import Path


if version("ai-dynamo") != "1.6.0" or version("vllm") != "0.32.0rc1.dev100+g77154ca61":
    raise SystemExit("Revalidate this adapter when either runtime changes")

path = Path("/usr/local/lib/python3.12/dist-packages/dynamo/vllm/embedding_worker_processes.py")
source = path.read_text()
expected = "21e953a23a3b27623361ade3fcc224d93b194599436206980c9d77bff7db260f"
if hashlib.sha256(source.encode()).hexdigest() != expected:
    raise SystemExit("Unexpected Dynamo embedding helper source")

# vLLM 0.32 changed the multi-client ZMQ binding API. Bolt is a generation
# worker and never uses the multi-process embedding path. Defer that import
# until the embedding function is actually invoked; do not alias the new API
# to the old name because their return types have different semantics.
tree = ast.parse(source)
function = next(
    node for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name == "create_shared_embedding_engine_client"
)
lines = source.splitlines(keepends=True)
after_docstring = function.body[0].end_lineno
lines.insert(after_docstring, "    from vllm.v1.engine.utils import get_engine_zmq_addresses\n")
patched = "".join(lines).replace(
    "from vllm.v1.engine.utils import get_engine_zmq_addresses, launch_core_engines",
    "from vllm.v1.engine.utils import launch_core_engines",
    1,
)
ast.parse(patched)
path.write_text(patched)
print("Dynamo 1.6: deferred unused multi-process embedding import for Bolt")
