# SPEC — GitHub-compatible git forge (github-core-clone-01)

A single-process HTTP server, written from scratch in Python 3.13 using ONLY the standard library,
in `main.py` at this repo root. It implements a GitHub-compatible forge: REST API v3, git smart
HTTP, a GraphQL API, and an MCP server. No third-party dependencies, no subprocesses calling the
`git` binary — implement pack/protocol handling in pure Python.

## 0. Boot contract (non-negotiable)

- On start, bind your OWN port: use `PORT` env var if set, else pick a free ephemeral port
  (`socket.bind(('127.0.0.1', 0))`). NEVER use 8080.
- Within 20 s of start, `POST {"port": <int>}` to `$MAKEFUN_ENV_URL/register` (if that env var is
  set; skip silently when absent so local dev works without it).
- `GET /healthz` must answer `200` (any JSON body) — it must answer twice within 10 s of start.
- Long-poll `GET $MAKEFUN_ENV_URL/done` (blocking, retries/timeouts tolerated) and exit 0 when it
  answers — only when MAKEFUN_ENV_URL is set.
- Read the bearer token from env `MAKEFUN_SHOP_TOKEN` if set. Every API request (REST, GraphQL,
  MCP, git) must carry `Authorization: Bearer <token>`; any non-empty bearer token is accepted as
  the viewer. A request WITHOUT an Authorization header gets `401` with a JSON body containing a
  string `message` field.

## 1. Seeded data

The grading scenario is deterministic; seed it into your store at startup. See
`data/scenario.json` (authoritative, dump of the kit's scenario):

- viewer login: `adaiversen` (id 1000); teammate `quinnkeller`; stranger `olgabauer`.
- org: `faint-meadow-org`.
- Seed exactly the 30 viewer repos listed in `scenario.json.repos` (fields: name, private,
  description, defaultBranch — create each as the viewer's repo with those properties).
- Also seed the 6 org repos (`orgRepos`) under the org, and 3 stranger repos (`strangerRepos`)
  under `olgabauer`. Repos carry a `node`-style global id for GraphQL (see §4).
- Do NOT pre-create issues or pull requests; checks create them at runtime. Numbers increment per
  repo starting at 1. Known labels for issues: the 11 entries in `scenario.json.labels` (list of
  strings). Creating an issue with a label not in that list must fail `422`.

## 2. REST API (`/api/v3`, GitHub-compatible)

All replies: `Content-Type: application/json`, a non-empty `X-GitHub-Request-Id` header, and
(when applicable) `X-OAuth-Scopes`. Error bodies are `{"message": "...", ...}` with proper status.
Malformed JSON body → 400 with `message`. Unknown route under /api/v3 → JSON 404 with `message`.

Endpoints (per check group; exact assertions in the kit checks, see "Authoritative references"):

- `GET /api/v3/` → 200 JSON root, with `X-OAuth-Scopes` header.
- `GET /api/v3/meta` → includes `installed_version` (string) among fields.
- `GET /api/v3/user/repos` → viewer's repos; supports `per_page` + `page` pagination of the 30
  seeded repos; `Link` header with `rel="next"` when more pages remain.
- `POST /api/v3/user/repos` `{name, private?, description?, ...}` → 201; created repo echoes
  `name`, `private`, `default_branch`. Duplicate name → 422.
- `GET /api/v3/repos/{owner}/{repo}` → repo object (404 JSON if missing; a repo name under a
  different owner is 404 for this viewer).
- `PATCH /api/v3/repos/{o}/{r}` `{description}` → updates description.
- `DELETE /api/v3/repos/{o}/{r}` → 204; later GET is 404.
- `GET /api/v3/repos/{o}/{r}/branches` → list of branch objects (`name`, `commit.sha`).
- `GET /api/v3/repos/{o}/{r}/commits?sha={branch}` → commit list reflecting pushes; commits on
  non-default branches appear when queried by branch.
- `POST /api/v3/repos/{o}/{r}/git/refs` `{ref, sha}` → creates a ref (used by branch checks).
- Contents:
  - `PUT /api/v3/repos/{o}/{r}/contents/{path}` `{content(base64), message, branch?, sha?}` →
    creates/updates a file with a commit; response includes `content` (base64) and `sha`.
  - `GET /api/v3/repos/{o}/{r}/contents/{path}` → file: `{name, path, sha, content(base64),
    encoding:"base64", type:"file"}`; directory: JSON array of entries `{name, path, type,
    sha, ...}`. Missing → 404.
  - `PUT` with a stale/wrong `sha` → 409.
  - `DELETE /api/v3/repos/{o}/{r}/contents/{path}` `{message, sha, branch?}` → removes the file
    with a commit; later GET is 404.
- Issues:
  - `POST /api/v3/repos/{o}/{r}/issues` `{title, body?, labels?}` → 201, `number` increments by 1
    per repo (shared with pull numbers), `state: "open"`, `labels` attached (unknown label → 422).
  - `GET .../issues?state=open|closed|all&sort=created&direction=asc|desc` — filter and order;
    `state=open` excludes closed issues. Objects use the GitHub issue shape: `number, title, body,
    state, labels:[{name}], user:{login}, created_at, updated_at, comments, html_url, id, node_id`.
  - `GET .../issues/{n}` → the issue; `PATCH .../issues/{n}` `{state:"closed"}` → closes,
    persists.
  - `POST .../issues/{n}/comments` `{body}` → 201 comment `{id, body, user, created_at}`;
    `GET .../issues/{n}/comments` lists them.
- Pulls:
  - `POST /api/v3/repos/{o}/{r}/pulls` `{title, body?, head, base}` → 201 open pull; `head.sha`
    = head branch tip, `base.sha` = base branch tip; unknown head branch → 422. Duplicate open
    pull for same head/base → 422.
  - `GET .../pulls`, `.../pulls?state=all|open|closed`, `.../pulls/{n}` — GitHub pull shape:
    `number, title, body, state, merged, head:{ref,sha,repo?}, base:{ref,sha}, user, created_at,
    mergeable, html_url`. Unknown number → 404.
  - `GET .../pulls/{n}/files` → array of `{filename, status:"added"|"modified"|"removed",
    additions, deletions, changes, sha, contents_url}`; multiple files supported.
  - `PUT .../pulls/{n}/merge` `{commit_title?, merge_method?}` → 200 `{sha, merged:true,
    message}`; creates a merge commit on the base branch (default: merge commit; content of head
    appears on base afterwards); HEAD of base advances. Merge with conflict (base changed since
    branch point so files conflict) → 405 `{message}`; unknown pull → 404. Merge closes the pull
    (`state:"closed"`, `merged:true`).

Repository object shape (GitHub-like): `id, node_id, name, full_name, private, owner:{login,id,
avatar_url}, html_url, description, default_branch, created_at, updated_at, pushed_at, url`.

## 3. Git smart HTTP (v0 + v2)

Routes: `GET /{owner}/{repo}.git/info/refs?service=git-upload-pack|git-receive-pack` and
`POST /{owner}/{repo}.git/git-upload-pack` / `git-receive-pack`. Missing repo → 404 for both
services. Content types: `application/x-git-upload-pack-advertisement`,
`application/x-git-upload-pack-result`, `application/x-git-receive-pack-advertisement`,
`application/x-git-receive-pack-result`.

- v2 (`Git-Protocol: version=2` header on info/refs): capability advertisement starting with
  pkt-line `version 2`, listing capabilities `agent`, `ls-refs`, `fetch`, `object-format=sha1`,
  then answer `ls-refs` requests (with `ref-prefix` filtering, `peel`, `symrefs` — HEAD symbolic
  ref resolved) and `fetch` requests (want/have negotiation, `done`, `shallow`/`deepen`/
  `deepen-since`? — at least depth-1 shallow and deepen; `include-tag`; thin-pack tolerable but
  safest to send complete packs; `sideband-all` optional).
- v0 info/refs: ref advertisement — `# service=git-upload-pack` pkt-line, flush, null-terminated
  first ref line, then refs (`refs/heads/...`, `refs/tags/...` with peeled `^{}` lines for
  annotated tags), capabilities line (`agent=`, `ofs-delta`, `side-band-64k`, `report-status`,
  `report-status-v2`, `delete-refs`, `quiet`, `atomic`, `object-format=sha1`). Handle
  `want`/`have`/`done` negotiation; reply packfile (v2 pack format, with SHA-1 checksum trailer,
  support ofs-delta objects in clients' requests, emit correct packs including deltas optional —
  integrity of objects is what's verified).
- receive-pack: advertise refs incl. zero-id (delete) handling; parse commands `old new ref`,
  handle create (`0{40} new`), delete (`old 0{40}` — only with `delete-refs` capability), update,
  non-fast-forward refusal (verify old == current tip ancestry; refuse with `ng <ref>
  non-fast-forward` in report-status, and do NOT apply that ref), mixed reports (some ok, some
  ng), `report-status` / `report-status-v2` reply (`unpack ok`, then per-ref `ok`/`ng` lines,
  over side-band-64k band 1 when requested; band 2 progress optional/empty). A malformed
  pkt-line POST body → HTTP 400 (and the repo stays healthy afterwards). Empty-push edge cases:
  no-op fetch (nothing new) must still return a valid (empty, header + trailer) pack.
- Shallow: support `--depth=1` clone and `--deepen` fetches — emit `shallow <oid>` lines and
  correct boundary handling; shallow file tracked per repo/session.
- Everything byte-exact where git itself is byte-exact: object hashes (git blob/tree/commit/tag
  formats — author/committer lines with timestamps and timezones), tree entry names/modes, tag
  objects, annotated tag peeling. Pushing 100 files must work (pack may be large).

## 4. GraphQL (`POST /api/graphql`)

JSON request `{query, variables?, operationName?}` (also support operation-name selection).
Responses: HTTP 200 with `{data, errors?}`; invalid/unknown FIELD → 200 with `errors` array and
partial/null data; unauthenticated (no Authorization) → HTTP 401. Support TWO operations in one
JSON array body (batch), answered in order.

Schema surface required:
- `Query.viewer { login }` (token owner = viewer login).
- `Query.repository(owner:, name:) { id, databaseId, name, nameWithOwner, owner { login },
  defaultBranchRef { name, target { ... on Commit { oid } } }, viewerPermission,
  hasIssuesEnabled, isPrivate, description }`. Unknown repo → `data.repository = null`, NO errors.
- `Query.node(id:)` with `... on Repository { id name nameWithOwner }` — Relay global ids are
  opaque strings (base64 of e.g. `Repository:<id>` recommended), stable across reads.
- `Query.__type(name:)` introspection: `Issue`, `PullRequest`, `Repository` types list expected
  fields (see kit check: Issue fields gh feature-detects — include `number,title,state,body,
  author,labels,createdAt,updatedAt,comments,closed`; SearchType enum must exist). Implement
  standard introspection machinery (`__schema { types { name } }`) at least minimally.
- `Repository.issues(first:, after:, states:[OPEN|CLOSED], orderBy:{field:CREATED_AT,
  direction:ASC|DESC})` → `Connection { nodes { number title state createdAt body } pageInfo {
  hasNextPage endCursor } }` with cursor pagination to the end.
- `Repository.issue(number:)` → `Issue { id number title state body author { login }, labels (
  first:) { nodes { name } }, comments(first:) { nodes { body author { login } } } }`.
- `Repository.pullRequests(first:)` → nodes with `number, title, headRefName, baseRefName,
  mergeable (MERGEABLE/CONFLICTING), state (OPEN/MERGED/CLOSED)`; `pullRequest(number:)` with
  `files(first:) { nodes { path, additions, deletions } }` and `mergeable`, `state`.
- `Mutation.createIssue(input:{repositoryId?, repositoryName?, owner?, title, body, labels?})`
  → `{ issue { id number title } }` — visible via REST afterwards.
- `Mutation.closeIssue` / issue `state:CLOSED` via `updateIssue` if present in check → REST sees
  closed. (Read the kit check bodies for exact mutation names/inputs.)
- `Mutation.createPullRequest(input:{baseRefName, headRefName, title, body?, repositoryId?})`.
- `Mutation.mergePullRequest(input:{pullRequestId?, repositoryNameWithOwner?, number?,
  query?: mutationGraphQL, mergeMethod: SQUASH})` → `{ pullRequest { merged, state, ... } }`;
  REST view shows merged: true.
- `Mutation.addComment(input:{subjectId?, body})` → comment visible via REST.

GraphQL pull-request checks need a git fixture: before them, the checker creates a repo via REST
and pushes branches with `git` over your smart HTTP — so git + REST must be solid first.

## 5. MCP server (`POST /mcp`)

Streamable-HTTP JSON-RPC 2.0:
- `initialize` → result `{protocolVersion, capabilities:{tools:{}}, serverInfo:{name, version}}`;
  requires Authorization (no token → 401).
- `notifications/initialized` accepted (202/200/204).
- `tools/list` → `{tools:[...]}` including tools named at least: `create_issue`,
  `list_issues`, `create_pull_request`, `merge_pull_request` (check exact names + input schemas
  in the kit's checks-mcp.ts). Stable across repeated calls. Idempotent `tools/list` after
  `initialize` with the SAME session id must work (session via `Mcp-Session-Id` header or
  stateless — but a returned session id must be honored).
- `tools/call` `{name, arguments}` → `{content:[{type:"text",text:"..."}]}`; creating an issue /
  pull through MCP is visible via REST (parity). `merge_pull_request` merges like REST merge.
- Unknown tool → JSON-RPC error `method/code -32602` (invalid params) or `tools/call` result with
  `isError:true` (match the kit's exact expectation). Malformed JSON body → JSON-RPC parse error
  `-32700`. Missing required arguments → invalid-params error. Several concurrent calls must all
  succeed.

## 6. Authoritative references (READ THESE — they are the checker, they win over this SPEC)

- Kit check sources: `.kit-ref/`
  (`checks-git.ts`, `checks-rest.ts`, `checks-graphql.ts`, `checks-mcp.ts`, `wire.ts` for the
  scenario/client wire formats, `git-client.ts`, `rest-client.ts`, `graphql-client.ts`,
  `mcp-client.ts` for exactly how requests are made).
- The full check list (id + title for all 120 checks): `data/checks.tsv`.
- Scenario data: `data/scenario.json`.

## 7. Dev loop (local conformance driver)

```bash
# terminal 1: serve (pick any port, e.g. 18081)
python3 main.py --port 18081
# terminal 2: run the kit's own checks against it
node --experimental-strip-types --no-warnings \
  .kit-ref/run_checks.mts \
  http://127.0.0.1:18081 devtoken
```

Target: `TOTAL >= 100/120`. Iterate: run the driver, fix failures, re-run. Suggested build order:
REST repos/issues/pulls first (35 checks), then git smart HTTP (30), protocol (5), then GraphQL
(20, needs git fixture working), then MCP (15), then bench-friendly performance (fast JSON,
connection reuse).

## 8. Hard constraints

- Python 3.13, stdlib only, entrypoint `main.py` in repo root, single process is fine (threads ok).
- No reading of `/workspace/makefun/gitforge` (an unrelated prior attempt) — implement from this
  spec and the kit sources only.
- The server must be robust: a failed request must never kill the process; every check runs
  sequentially against the same live instance.
