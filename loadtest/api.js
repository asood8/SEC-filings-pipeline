// Load test for the public read API (Phase 8): `make loadtest`, against a running API.
//
// Two scenarios run together: readers browsing (filing lists, filing pages, companies) at a rising
// request rate, and a steady stream of searches. Thresholds fail the run if errors or latency
// regress. The API's per-client rate limit would turn most requests into 429s from one machine,
// so run the API with it raised (`make loadtest` explains how).
import { check } from 'k6'
import http from 'k6/http'
import { Rate } from 'k6/metrics'

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000'
// Seconds per stage; LOADTEST_SCALE=0.2 makes a quick smoke run.
const SCALE = Number(__ENV.LOADTEST_SCALE || 1)
const seconds = (n) => `${Math.max(1, Math.round(n * SCALE))}s`

const QUERIES = [
  'insider selling',
  'new chief financial officer',
  'debt offering',
  'quarterly results',
  'share repurchase',
  'Tim Cook',
  '"10b5-1"',
  'cybersecurity incident',
  'leadership change',
  'dividend',
]

// Share of responses served from the API's Redis cache (X-Cache: HIT).
const cacheHits = new Rate('cache_hits')

export const options = {
  scenarios: {
    browse: {
      executor: 'ramping-arrival-rate',
      exec: 'browse',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 20,
      maxVUs: 200,
      stages: [
        { target: 25, duration: seconds(30) },
        { target: 100, duration: seconds(60) },
        { target: 100, duration: seconds(30) },
        { target: 0, duration: seconds(15) },
      ],
    },
    search: {
      executor: 'constant-arrival-rate',
      exec: 'search',
      rate: 5,
      timeUnit: '1s',
      duration: seconds(135),
      preAllocatedVUs: 10,
      maxVUs: 100,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:list}': ['p(95)<300'],
    'http_req_duration{kind:detail}': ['p(95)<300'],
    // An uncached search waits at most EMBEDDING_QUERY_TIMEOUT_SECONDS (2 s) for the embedding
    // model, then answers from keywords alone; the rest of the request is fast.
    'http_req_duration{kind:search}': ['p(95)<2500'],
  },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
}

export function setup() {
  const filings = http.get(`${BASE_URL}/filings?limit=100`)
  const companies = http.get(`${BASE_URL}/companies?watchlist=true&limit=200`)
  if (filings.status !== 200 || companies.status !== 200) {
    throw new Error(`API not ready at ${BASE_URL}: ${filings.status}/${companies.status}`)
  }
  return {
    accessions: filings.json('items').map((item) => item.accession_no),
    tickers: companies.json('items').map((item) => item.ticker),
  }
}

const pick = (items) => items[Math.floor(Math.random() * items.length)]

function get(path, kind) {
  const response = http.get(`${BASE_URL}${path}`, { tags: { kind, name: kind } })
  check(response, { 'status is 200': (r) => r.status === 200 })
  cacheHits.add(response.headers['X-Cache'] === 'HIT')
  return response
}

export function browse(data) {
  const roll = Math.random()
  if (roll < 0.35) get('/filings?limit=25', 'list')
  else if (roll < 0.75) get(`/filings/${pick(data.accessions)}`, 'detail')
  else if (roll < 0.85) get('/companies?watchlist=true', 'list')
  else get(`/companies/${pick(data.tickers)}`, 'detail')
}

export function search() {
  // Most searches use the default hybrid mode; some ask for keywords only.
  const mode = Math.random() < 0.7 ? 'hybrid' : 'keyword'
  get(`/search?q=${encodeURIComponent(pick(QUERIES))}&mode=${mode}`, 'search')
}
