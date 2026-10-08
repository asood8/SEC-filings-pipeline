# ADR-0011: Search and the public API

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Phase 3 makes filings searchable, by keyword and by meaning, and rounds out the read API:
companies, filing detail, pagination, caching, and rate limiting.

The brief chose Postgres for both kinds of search (full-text search plus pgvector), so no separate
search engine is needed. Still open were the embedding model (brief §12: pick a reasonable
default), what exactly gets indexed, how to combine the two rankings, and how the API behaves when
Redis or the embedding model is down.

## Decision

### What is indexed

- **One search document per filing** (`filing_search`), built from the filing's **current**
  summary: the headline, the company name, the ticker, and the body (summary, key points, and a
  plain-English form label such as "Form 4 insider trading report", so "insider" finds Form 4s).
- **Keywords (added 2026-10-08):** other words for what the text says. They are searched, but
  never embedded or shown.
  - *Abbreviations both ways:* a document that has only one of "CFO" and "Chief Financial
    Officer" gets the other. The list holds 12 unambiguous ones, such as CEO, EVP, RSU, and EPS.
    CIO (information or investment officer?) and CLO are left out. Abbreviations count only in
    capitals, so the name "Cao" doesn't count as CAO.
  - *Irregular verbs:* the Form 4 template writes "bought" (code P) and "sold" (code S), which
    stemming doesn't relate to "buy", "purchase", "sell", or "sale". Those words get added.
  - *Weight B, like the body:* an alias counts as much as the words it stands for. With weight C,
    the Chevron 8-K for "CFO appointment" ranked third in hybrid search instead of first. No other
    labeled query changed.
  - *Why at index time:* Postgres can also expand queries (`ts_rewrite` with rules). But the verb
    forms belong to documents anyway, the reverse direction ("chief financial officer" finding
    "EVP & CFO") needs no phrase rules, and queries stay plain `websearch_to_tsquery`. The cost:
    changing the lists needs a `filings-index` rebuild, which takes seconds and embeds nothing.
  - *Why not in the embedded text:* existing embeddings stay valid, so nothing is re-embedded,
    and the model sees only what the summary says.
- **Written with every summary.** The summarizer writes the document in the same transaction as
  the summary. `filings-index` rebuilds documents and backfills embeddings, idempotently.

### Keyword search

- **Postgres full-text search** over a generated, weighted `tsvector`: headline and company weigh
  more than the body, with a GIN index.
- **Web-search syntax.** Queries go through `websearch_to_tsquery`, so quotes, `OR`, and `-word`
  work as people expect.
- **Relaxed fallback.** If a query without that syntax matches nothing with all its words, it is
  retried matching any word, ranked so filings matching more words score higher. Postgres would
  otherwise return nothing for natural-language queries like "CFO appointment" (no filing has
  both words: Chevron's 8-K says its new CFO was elected).
- **Tickers are indexed apart from words (added 2026-10-08).** The English configuration indexed
  Costco's ticker COST as the word "cost". "costs" stems to "cost" too, so "restructuring to cut
  costs" returned Costco Form 4s first.
  - *Index:* where the ticker stands alone in capitals in the headline or body, it is blanked out
    (the Form 4 template writes "100 COST shares"). It is added once as a lexeme in capitals,
    `'COST'`, with weight A. Normalized words are lowercase, so no word can match that lexeme.
  - *Query:* a query word shaped like a ticker (up to 5 letters and digits, maybe a class such as
    BRK-B) also matches tickers through `ts_rewrite`: `'cost'` becomes `'cost' | 'COST'`, which
    keeps phrases and `-word` working. This happens when the word is typed in capitals (COST,
    AAPL), or in any case when no filing has it as a word: "aapl", "nvda", and "cat" find Apple,
    Nvidia, and Caterpillar. "cost" and "costs" stay words. One small query checks the
    lowercase words first, so the search itself keeps a constant tsquery that Postgres computes
    once, not once per row. A 135-character query takes 3.7 ms instead of 1.2 ms.
  - *Alternatives:*
    - Indexing the ticker with the `simple` configuration doesn't work: the stemmed query word
      "costs" is "cost", which still matches.
    - Matching tickers case-insensitively against the companies table would turn everyday words
      into tickers: it lists 8,016 of them, among them ALL, CAT, COST, and FUN.
    - Treating a ticker in capitals as only a ticker would need a lookup per query, and would
      lose the word for a ticker that is also an acronym.
  - *Limits:*
    - A ticker that is an English stop word (IT, ALL, T) can't be searched, as before.
    - Inside quotes, a ticker no longer matches where it stood: `"COST shares"` finds nothing.
    - The lowercase rule depends on the documents: a lowercase ticker that later appears as a word
      in some summary stops matching the ticker. In capitals, it still does.
  - *Schema:* `filing_search` gains `ticker` and `keywords` columns, and `company` holds the name
    only (migration `23e9e08283f1`; `ALTER COLUMN ... SET EXPRESSION` needs Postgres 17+). The
    embedded text is unchanged ("name ticker"), so existing embeddings stay valid. Rows written
    before the migration keep the old behavior until `filings-index` rebuilds them, so a
    deployment runs it once after the migration.

### Semantic search

- **Model:** `nomic-embed-text:v1.5` (768 dimensions, 274 MB), served by the same Ollama container
  through the OpenAI-compatible `/embeddings` endpoint. It uses the model's
  `search_document:` / `search_query:` task prefixes.
- **Storage:** vectors live in `filing_embeddings`, keyed by (filing, model), with an HNSW cosine
  index.
- **No failover between embedders.** Vectors from different models aren't comparable, so one
  configured model embeds both documents and queries. It is opt-in through `EMBEDDING_MODEL`.

### Hybrid search, the default

The top 50 results from each method are merged with **reciprocal rank fusion**: each list adds
1/(60 + rank) per result. RRF needs no calibration between BM25-style ranks and cosine scores.

- **A Form 4's semantic rank counts half (added 2026-10-08):** it adds 0.5/(60 + rank).
  - *Why:* Form 4 documents come from one template and are 92% of the filings, so they fill the
    semantic top 50 for almost any query. Any of them that also matched a keyword got both
    contributions and outranked the 8-K being looked for.
  - *Measured* with `filings-search-eval` (below): MRR went from 0.90 to 0.94, recall@10 from
    0.89 to 0.91, and the Form 4 share of the top 5 from 54% to 30%. No labeled query got worse.
    A weight of 0.75 also helps; 0.25 starts to hurt. Spot checks of 14 unlabeled queries (names,
    and Form 4 topics such as "insider selling" and "phantom stock") changed a slot or two at
    most, and Form 4s stayed on top where they were what the query asked for.
  - *Filters:* a `form_type` filter turns the weight off. With one form left, it would only
    tilt keyword results against semantic ones.
- **One implementation:** `hybrid_search` lives in filings-common, so the API and the evaluation
  run the same code.

### Measuring search quality (added 2026-10-08)

`filings-search-eval` runs a labeled query set, `config/search_eval.json`, through keyword,
semantic, and hybrid search. It uses the API's own search functions, so the measured ranking is
the production ranking.

- **Metrics, per mode:**
  - *Recall@5 and recall@10:* the share of a query's relevant filings that appear in its top 5 or
    top 10.
  - *MRR (mean reciprocal rank):* 1 if the first relevant filing is ranked first, 1/2 if second,
    and so on, averaged over queries. It counts 0 if no relevant filing is in the top 20, the
    API's default page.
  - *Form 4 share@5:* how many of the top 5 slots Form 4s take, counted only for queries that look
    for other forms.
- **Output:** a Markdown table. `--per-query` adds one row per query, and `--json` saves the top
  10 of every query so two runs can be compared.
- **Read-only:** it opens read-only transactions. Without a working embedding model, it skips the
  semantic and hybrid modes and still measures keyword search.
- **How the labels were made:** 20 queries, judged against the local database (277 filings filed
  2026-09-08 to 2026-10-06). The judging read every 8-K's stored summary, and every Form 4's
  headline and transaction codes; search results were not used. Each query lists every filing
  judged relevant, with a note saying why.
  - The queries cover the examples tested below, words the summaries don't use ("CFO",
    "purchase", "electric"), names, and Form 4 topics.
  - The labels fit that database only. Run against another one, the tool lists the labeled filings
    it can't find.
- **Use:** run it before and after a ranking change (fusion, document text, embedding model).
  Keep the change if the metrics improve and no query gets much worse. Twenty queries make a smoke
  test, not a benchmark: one query moves a mean by 0.05.
  - *A change to the search documents* can be measured on a copy of the database without touching
    the live one: restore a `pg_dump` into a throwaway database, migrate it, run `filings-index`,
    and point `DATABASE_URL` at it. The embeddings come with the copy, so only a change to the
    embedded text needs re-embedding.
  - *One change at a time:* the 2026-10-08 changes were measured separately (each alone moved
    only the query it targeted), then together. Unlabeled spot checks compared the top 5 before
    and after: tickers in capitals and in lowercase, company and person names, and Form 4 topics.

### Degradation instead of failure

- **No embedder:** without a working embedder, semantic and hybrid requests fall back to keywords,
  and the response says `"degraded": true`.
- **A slow embedder (added 2026-10-07):** a k6 smoke test saw 7 s searches while the CPU model was
  busy embedding documents. A query embedding now waits at most `EMBEDDING_QUERY_TIMEOUT_SECONDS`
  (default 2 s; well under a second on an idle model), then the search answers with keyword results
  (`degraded: true`). httpx2 applies the timeout to connecting and to each read, and the model
  sends its answer in one piece, so it bounds the wait for the answer. Documents (the summarizer,
  `filings-index`) keep `EMBEDDING_TIMEOUT_SECONDS` (60 s). A fallback caused by a failing model is
  sent with `Cache-Control: no-store`, so the response cache doesn't keep serving it after the
  model recovers; with no embedder configured, keyword results are all there is and are cached as
  usual.
- **Embedding failure in the summarizer:** this never blocks a summary. The filing stays
  keyword-searchable and `filings-index` embeds it later.

### API

- **Endpoints:** `GET /search`, `GET /filings` (keyset cursors, current headline),
  `GET /filings/{accession_no}` (current summary with provenance and grounding flags, Form 4 key
  fields, 8-K items, EX-99 exhibit links), `GET /companies` (watchlist filter, name or ticker
  search, cursors), and `GET /companies/{ticker}` (recent filings).
- **Insider trades (added 2026-10-08):** a Form 4's detail lists its transaction lines, and
  `GET /companies/{ticker}/insider-activity?days=90` (1 to 365) totals insiders' purchases and
  sales (codes P and S) of the company's stock per reporting owner. Both read
  `form4_transactions` (ADR-0006).
- **Docs:** typed response models generate the OpenAPI docs at `/docs`.
- **Keyset cursors** encode the last row's sort key, so pages stay correct while new filings
  arrive. Offsets would repeat or skip rows.
- **Rate limiting:** per client through Redis. It's a non-blocking token bucket (default 60/minute,
  burst 20) that returns 429 with `Retry-After`. `CF-Connecting-IP` is honoured only when
  `API_TRUST_PROXY_HEADERS` is set, because otherwise clients could spoof it.
- **Caching:** successful JSON GETs are cached for 60 s. New filings arrive minutes apart, so a
  short TTL needs no invalidation.
- **Query embeddings are cached too** (added 2026-10-07), for 10 minutes, keyed by the model and a
  SHA-256 of the query: one vector serves every mode, filter, and page of a query, which the
  URL-keyed response cache can't. Each is 3 KB (32-bit floats, the precision pgvector keeps).
  Like the response cache it fails open, and `API_CACHE_TTL_SECONDS=0` turns it off.
- **Probes:** `GET /healthz` (liveness: the process answers) and `GET /readyz` (readiness: Postgres
  answers `SELECT 1` within 2 s, else 503) are never rate limited, cached, or traced (ADR-0017).
- **Security headers** (added 2026-10-08): every response, CORS preflights and cached responses
  included, carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and
  `Referrer-Policy: no-referrer` (URLs can carry search terms). The website sends its own
  (ADR-0015).
- **Redis failures are fast and fail open.** The API's Redis client has 0.5 s timeouts and no
  retries. After an error, Redis is skipped for 10 s, so an outage costs one timeout, not one per
  request. Before this change, an outage added about 8 s to every request (redis-py retries by
  default).

## Alternatives considered

- **A dedicated search engine (OpenSearch, Meilisearch).** Another always-on service on a 12 GB VM,
  for a few hundred documents a month. Postgres handles both kinds of search.
- **Searching raw filing text instead of summaries.** Better recall for rare terms, but much bigger
  documents, mostly legal boilerplate. Summaries are what users read and what results display.
- **Weighted score blending instead of RRF.** It needs scores calibrated across very different
  scales, and would drift whenever the model changes.
- **Gemini embeddings.** They would tie query-time search to the free tier's quota and an external
  call. A local model makes query embedding free and private.
- **Offset pagination.** Simpler, but unstable while filings are inserted.

## Consequences

- **Live evaluation on the 275 real filings** (keyword, semantic, and hybrid side by side):
  - Semantic search finds meaning: "leadership change" → Chevron's officer changes and PepsiCo's
    board election; "insider selling" → Form 4 sales; "trading plan cancelled" → Oracle's 8-K;
    "acquisition" → AMD's World Labs deal.
  - Keyword search is exact for names and phrases ("Warren Buffett", "quarterly earnings results").
  - After the relaxed fallback, hybrid ranks Amazon's note offering first for "debt offering".
- **Known weaknesses, for the Phase 8 benchmark.** Short two-word queries give the small embedding
  model nearly flat scores (0.53–0.55 for "debt offering"), and with 255 of 275 documents being
  Form 4s, those crowd the semantic results. Some hybrid results after the first few are weak
  ("leadership change" → an ESPP amendment).
- **Measured on 2026-10-08** with `filings-search-eval` (277 filings, `nomic-embed-text:v1.5`,
  the Form 4 weight in place):

  | Mode | Recall@5 | Recall@10 | MRR | Form 4 share@5 (16 queries) |
  |---|---|---|---|---|
  | keyword | 0.72 | 0.72 | 0.83 | 19% |
  | semantic | 0.81 | 0.87 | 0.85 | 49% |
  | hybrid | 0.86 | 0.91 | 0.94 | 30% |

  What the per-query rows showed, for later work:
  - *Keyword search misses other wording.* "CFO appointment" fails because the summaries say
    "Chief Financial Officer". "insider purchase" fails because Form 4s say "bought". Semantic
    search finds both.
  - *Hybrid can be worse than semantic alone.* For "insider purchase", hybrid's recall@10 is 0.17
    against semantic's 0.83: wrong keyword matches, such as Nike's employee stock *purchase* plan,
    outweigh the right semantic ones.
  - *Tickers collide with words.* Costco's ticker COST stems like "costs", so keyword search
    returns Costco Form 4s first for "restructuring to cut costs".
  - *Semantic mode on its own still gives Form 4s half the top 5.* The weight applies only to
    hybrid search, the default.
- **After keywords and separate tickers (2026-10-08)**, on a copy of the same database (278
  filings, one more 8-K, which no label involves; the "before" numbers are the same on it):

  | Mode | Recall@5 | Recall@10 | MRR | Form 4 share@5 (16 queries) |
  |---|---|---|---|---|
  | keyword | 0.72 → 0.86 | 0.72 → 0.87 | 0.83 → 0.94 | 19% → 11% |
  | semantic | 0.81 | 0.87 | 0.85 | 49% |
  | hybrid | 0.86 → 0.90 | 0.91 → 0.95 | 0.94 → 1.00 | 30% → 28% |

  Three queries changed, each through the change aimed at it, and no other labeled query moved:

  | Query | Change | Keyword: recall@10, first relevant | Hybrid: recall@10, first relevant |
  |---|---|---|---|
  | CFO appointment | abbreviations | 0 → 1.00, none → 4th | 1.00 → 1.00, 5th → 1st |
  | insider purchase | verb forms | 0 → 1.00, none → 1st | 0.17 → 1.00, 2nd → 1st |
  | restructuring to cut costs | tickers | 0 → 1.00, 11th → 1st (Form 4s in the top 5: 5 → 0) | 1.00, 1st (Form 4s: 2 → 0) |

  - *Spot checks:* tickers still find their company's filings (AAPL, aapl, NVDA, nvda, COST, META,
    V, BRK-B), and names find the same filings as before (Apple, Costco, Tim Cook, Warren Buffett,
    Berkshire). "insider selling" now returns only sales, where it used to mix in grants and
    Oracle's plan cancellation. "insider buying" returns the open-market purchases, where it used
    to return option grants ("right to buy"). "cost" no longer returns Costco Form 4s.
  - *Semantic mode, unchanged:* re-ranking its top 50 the way hybrid weighs Form 4s lowered their
    share of the top 5 to 35–40% at weights 0.85–0.9. But "insider purchase", a Form 4 query, got
    worse (recall@5 0.67 → 0.50), and MRR fell at 0.8 and below. Someone choosing semantic mode
    also asks for plain similarity, so that mode stays as it is.
- **Changing dimensions needs a migration.** `EMBEDDING_DIMENSIONS` is fixed at 768 by the pgvector
  column, so a model with another size means a migration and a re-embed with `filings-index`.
- **Production.** The API needs the embedding server at query time, so Phase 7 must run it next to
  the API. A llama.cpp server serves one model, so that means an embeddings instance alongside the
  LLM, or Ollama. (ADR-0017 runs a second llama.cpp server for the embeddings.)
