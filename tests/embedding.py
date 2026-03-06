import asyncio

import litellm
from litellm.llms.bytez.common_utils import API_BASE

EMBEDDING_MODEL = "litellm_proxy/Embedding-3-Small"
BASE_URL = "http://litellm-internal.mc-k8s-apn1.notta.io"
TEXTS = [
    "embedding test: hello world",
    "embedding test: quick brown fox",
]
async def main() -> int:
    response = await litellm.aembedding(model=EMBEDDING_MODEL, api_base=BASE_URL, input=TEXTS)
    print(f"Response type: {type(response)}")
    print(f"Response: {response}")
    print(f"Response data type: {type(response.data)}")
    print(f"Response data: {response.data}")
    embeddings = [list(item.embedding) for item in response.data]
    if len(embeddings) != len(TEXTS):
        raise RuntimeError("Embedding result length mismatch")
    dim = len(embeddings[0]) if embeddings else 0
    print(f"ok: count={len(embeddings)} dim={dim}")
    return 0
if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))