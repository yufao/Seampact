"""Manual resource binding for the unchanged REX-RAG BM25 implementation.

Copy beside search_r1/ in a fixed source checkout before intake. The resident
owner has the same config/retriever lifetime as the original retrieval server.
This wrapper is preparation work, not an automatically discovered split.
"""

from search_r1.search.retrieval_server import Config, BM25Retriever


class REXBM25:
    def __init__(self, index_path: str, corpus_path: str, topk: int = 3):
        self.config = Config(retrieval_method="bm25", retrieval_topk=topk,
                             index_path=index_path, corpus_path=corpus_path)
        self.retriever = BM25Retriever(self.config)

    def search(self, query: str) -> list:
        return self.retriever.search(query)
