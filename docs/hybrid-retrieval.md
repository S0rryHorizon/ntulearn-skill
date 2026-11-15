# Optional local hybrid retrieval

The default `search` command retains exact, all-keyword matching. Natural-language
input can use `--match any` for BM25 candidate retrieval. An optional local BGE
v1.5 encoder adds semantic candidates, fused with the top 100 BM25 results using
reciprocal rank fusion (k=60). The final candidate list is bounded to 100 items.

```console
python -m pip install -e '.[retrieval]'
ntulearn search 'How does water vapor affect silicon oxide growth?' --match any
ntulearn search 'How does water vapor affect silicon oxide growth?' --match any \
  --embedding-model /absolute/path/to/local/bge-small-en-v1.5 --current-only
```

Provision model files separately. Search only loads the supplied local directory,
with remote custom code disabled; it does not download weights or send queries to
an API. The tested English model is `BAAI/bge-small-en-v1.5` at revision
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. Chinese semantic retrieval has not been
evaluated with this model; bilingual keyword decomposition remains applicable.

The CLI stores `retrieval-embeddings.npz` beside the private runtime database.
Never commit or publish that file. The cache contains vectors and a fingerprint,
is written atomically with private file permissions, and is rebuilt when scoped
text or the model changes. No pickle loading is used. In-memory vectors are reused
by a persistent service; a cold corpus still requires embedding computation.

Python integrations may construct `HybridSearchService(database, encoder)` and
assign it to `CoreService.search_service`. Use a `SearchQuery` with
`match_mode="any"`; `all` retains lexical behavior. Core pagination binds both the
matching mode and the model/ranking profile. Course, current-version, document-kind
and other existing filters apply to both candidate channels before fusion. Source
references and page/paragraph locators remain those of the underlying documents.
If a sync replaces documents between reads, fusion excludes keys absent from the
dense snapshot; surviving candidates retain their original rank contributions.

Hybrid CLI JSON includes a `warnings` entry with code `hybrid_candidates` and the
message `Found N hybrid candidate(s); verify relevance in the cited source.` This
also marks empty hybrid results. Lexical output, including `all`, is unchanged.

The flat local index is bounded to 50,000 documents per selected scope. It is not an
approximate vector database. Relevance scores are ranking signals, not calibrated
answer probabilities: retrieved candidates still require source verification, and
an irrelevant question can produce candidates. Neither this module nor its retrieval
benchmark measures generated-answer accuracy.
