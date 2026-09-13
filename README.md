# Claude referral checker

Checks the read-only validity endpoint used by Claude's own referral page:
`GET https://claude.ai/api/referral/code/CODE`. A matching code with the boolean
`is_valid: true` is saved to `found_code.txt`. Availability is checked at request
time; account eligibility and redemption still need confirmation in your browser.
The checker never redeems a code.

## Setup

Python 3.10 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py --help
```

## Check your links simultaneously

The 25 supplied links are in `links.txt`.

```sh
.venv/bin/python run.py --file links.txt --workers 4 --interval 2
```

This checks the whole list and saves every valid link. There can be four requests
in flight, but request starts are spaced at least two seconds apart **across all
workers**. Concurrency helps when responses are slow. `--interval` controls the
overall request rate. All workers share one session and a bounded connection pool.
TCP keepalive probes start after 30 idle seconds, with 15 seconds between probes
where supported by the operating system. Connections idle for more than 30 seconds
are not reused for new requests.

The file accepts one URL or code per line, blank lines, and `#` comments.
Duplicates are skipped; query strings and fragments are removed.

```sh
.venv/bin/python run.py https://claude.ai/referral/EMvVYtsAog
```

## Validate shared links on multiple devices

`distributed.py` provides a coordinator and workers for a **finite list of supplied
referrals**. It does not generate codes. Each worker fetches the API response;
the coordinator performs strict validation before recording a valid result.
A generic HTTP 200 referral page is not proof that a link works. The older script
that saved any page without an invalid phrase could produce false positives.

Copy this project to each device and install `requirements.txt` in its virtual
environment. Choose one computer to keep the coordinator running. Stop standalone
`run.py` processes before starting distributed work: they do not share this queue's
pacing. Use a separate state file, `distributed.sqlite3`, as shown below.

Generate one secret on the coordinator, once:

```sh
.venv/bin/python -c "import secrets; from pathlib import Path; p=Path('.coordinator-token'); f=p.open('x'); p.chmod(0o600); f.write(secrets.token_urlsafe(32)); f.close()"
export REFERRAL_COORDINATOR_TOKEN="$(cat .coordinator-token)"
.venv/bin/python -u distributed.py coordinator --file links.txt --interval 3 --workers 4
```

The token file is ignored by Git. Securely copy the same token file to each worker
device. Do not generate a different token on each device. `--workers 4` is the
maximum number of jobs leased out at once, across all devices. The coordinator
assigns at most one new job every three seconds globally. Network transit and device
scheduling affect the exact time a worker starts its request. Multiple workers help
overlap slow responses; they do not multiply that global rate.

The coordinator listens only on `127.0.0.1:8765`. On each remote device, connect
through SSH forwarding (replace the example SSH destination with your coordinator's
actual username and address; SSH access must already be configured):

```sh
ssh -N -L 8765:127.0.0.1:8765 YOUR_USER@COORDINATOR_HOST
```

Keep the tunnel open. In a second terminal on each worker device, in the project
directory:

```sh
export REFERRAL_COORDINATOR_TOKEN="$(cat .coordinator-token)"
.venv/bin/python -u distributed.py worker
```

You can also run a worker directly on the coordinator computer without a tunnel.
The authenticated API is intended for local/tunneled access, not direct public
deployment. Coordinator redirects are rejected to avoid forwarding credentials.

From any configured device, view progress or export the shared results:

```sh
.venv/bin/python distributed.py status
.venv/bin/python distributed.py results > distributed-valid-links.txt
```

Results are committed to the coordinator's SQLite file before acknowledgement;
the `results` command exports one URL per valid job. It does not append to the
standalone checker's `found_code.txt`. Open valid results in your browser to confirm
eligibility and redemption. Re-running the coordinator with an expanded input file
adds only new links and preserves completed work. Workers exit when the list is done;
start them again after adding more links.

If a worker disconnects, its unfinished job becomes available after its 60-second
lease expires. Reassigned jobs reject results from the old lease. A lost result
acknowledgement retries the report without issuing another validation request.
Recovery can still cause a link to be checked twice if a worker completed a request
but could not deliver its result; the stored result remains unique.

HTTP 429 pauses new assignments across all devices for five minutes or the longer
server-requested `Retry-After`, and doubles the shared request interval once per
episode. Already issued jobs may finish. The cooldown and slower pace survive
coordinator restarts. Temporary network/server errors retry with shared backoff,
up to three total assignments for a job; server `Retry-After` is honored. Access
blocks, unexpected responses, or exhausted retries halt new assignments globally.
The halt remains saved on restart, and `status` explains why. Investigate the cause
before resuming; this version has no automatic reset of a saved halt.

Distributed tests exercise concurrent claims, lease recovery, authentication,
strict validation, duplicate reports, shared cooldowns, and restart persistence.
HTTP integration tests use temporary localhost servers and mock all Claude requests.

## Run overnight until a valid link is found

On this Mac, keep the terminal open and run:

```sh
caffeinate -i .venv/bin/python -u run.py --file links.txt --loop --hours 8 --workers 4 --interval 2 2>&1 | tee overnight.log
```

The loop starts with your supplied links, then generates more codes if needed.
It **stops as soon as the server reports a valid link**, after eight hours, or on
an access block, rate limit, or unrecognized response. A valid supplied link can
therefore stop this command immediately.
Use `--wait-on-rate-limit` below to wait through HTTP 429 instead of exiting.

Progress is committed to `search.sqlite3` after each request. Run the same command
again to resume. Previously invalid codes are skipped; interrupted and failed
checks are retried first. Use another `--state PATH` for a fresh search or to
recheck previously invalid codes. Ctrl+C stops the process. `caffeinate` prevents
idle sleep while the program runs; keep the laptop lid open.

Omit `--file links.txt` to generate codes from the start. Omit `--hours 8` to remove
the time limit. The loop still stops on success or an upstream block/error.
Random discovery is not practical, even overnight.

## Keep collecting after a valid result

Add `--collect` to keep saving valid links instead of stopping at the first one.
It works with `--loop`, a time limit, or a finite number of attempts. Existing
results are not appended again, and the summary distinguishes valid results from
newly saved links. The same output file should be used when resuming.

For a list of real referrals that people have shared with you:

```sh
.venv/bin/python run.py --file links.txt --collect --workers 4 --interval 2 --state collection.sqlite3
```

Supplied lists already collect every valid result by default; `--collect` also
enables this behavior for generated codes. Use `--loop --collect` to continue
generating after a match. With no `--hours`, that mode has no time limit.

HTTP 429 means requests are being rate-limited. Collection mode stops on 429 by
default, and always stops on access blocks and unrecognized responses. Do not restart repeatedly or
increase concurrency after a rate limit. No interval guarantees avoiding flags.
Random guessing has an extremely low expected yield; actual shared referrals are
the useful input for collecting valid links.

## Wait through HTTP 429 and resume automatically

Add `--wait-on-rate-limit` to keep the process alive during rate limits:

```sh
caffeinate -i .venv/bin/python -u run.py --loop --collect --wait-on-rate-limit --workers 4 --interval 10 --state overnight.sqlite3 2>&1 | tee -a overnight.log
```

On HTTP 429, all workers pause new requests and the affected code is retried after
the cooldown. Requests already in flight may finish. The wait is at least five
minutes and at least as long as the server's `Retry-After` value, whether that is
seconds or an HTTP date. The local wait stays at five minutes on every episode;
longer server-requested waits are always respected.
Overlapping 429 responses extend the same cooldown.

Without adaptive mode, the request interval also increases after a rate-limit
episode: it doubles up to 60 seconds, with a minimum of 10 seconds. An already slower
configured interval is never reduced. This reduces request pressure; it does not
guarantee that future requests will be accepted.

Cooldown, backoff, and the slower interval are saved in the SQLite state file.
Reusing that file on restart honors the remaining cooldown before any request,
even when the restarted command omits `--wait-on-rate-limit`. Existing saved
deadlines are preserved, including those created by older versions with escalating
waits; subsequent episodes use the fixed five-minute local wait. Restart a running
process to load this change. This option enables
`search.sqlite3` by default if no `--state` is supplied. Run one process with
multiple workers so they share the limiter. Ctrl+C and `--hours` still work while
waiting. HTTP 401/403, browser challenges, unexpected responses, and exhausted
network/server-error retries still end the run.

The HTTP behavior follows [429 Too Many Requests](https://www.rfc-editor.org/rfc/rfc6585.html#section-4)
and [Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#field.retry-after).
The local five-minute minimum is a choice made by this checker, not a published
Claude rate limit or a guarantee that requests will be accepted after five minutes. The checker does not rotate IPs or
identities to get around restrictions.

## Adjust speed automatically

Use `--adaptive` to adjust the shared request interval from observed responses:

```sh
caffeinate -i .venv/bin/python -u run.py --loop --collect --adaptive --workers 4 --state overnight.sqlite3 2>&1 | tee -a overnight.log
```

Stop the previous process with Ctrl+C before starting this command. File changes
do not update an already running Python process. Keep using the same state file
to preserve progress and any remaining cooldown.

Adaptive mode starts at a 2-second interval by default. After 30 consecutive
accepted checks, it shortens a slower interval by 10%: for example, with
`--interval 10`, 10 → 9 → 8.1 seconds, and so on.
Accepted checks include both valid and invalid codes; they indicate a recognized
server response, not a successful referral discovery. Errors reset the count,
and responses from an earlier pacing setting do not count toward a speed increase.
The interval never falls below two seconds, limiting starts to 30 per minute
across all workers. This is a local cap, not a published or guaranteed Claude limit.

HTTP 429 pauses all new requests using the cooldown described above, then retries
at an interval at least twice as long as the interval that triggered the limit.
Regular (non-adaptive) mode keeps its 10-second floor. Adaptive mode also raises
the learned minimum interval to 25% longer than the interval that failed. For
example, a 429 at 4 seconds resumes at 8 seconds after the cooldown, then can
gradually approach 5 seconds; it will not return to 4 seconds. Repeated rate
limits can increase the interval beyond 60 seconds. Access blocks and unexpected
responses still stop the run.

The learned minimum, current interval, and accepted-check counter are saved in
SQLite after each response. Restarting resumes that progress instead of restarting
the 30-check window. Once the cooldown has expired, the current saved interval
takes precedence over the historical rate-limit slowdown. A larger `--interval`
or learned minimum still slows the pace and resets the window. Active cooldowns
remain enforced. Older state files without saved pacing progress initially use
their last saved slowdown; new responses then establish resumable progress.
Startup prints the restored counter (for example, `Adaptive progress: 17/30`).
`--adaptive` enables
rate-limit waiting and `search.sqlite3` automatically. `--interval` sets its
starting pace and must be at least two seconds. For a finite list of shared
links, use `--file links.txt --adaptive --workers 4 --state collection.sqlite3`.

This adjusts request pacing; it cannot infer valid codes or guarantee an optimal
rate. Limits may depend on more than request frequency and can change over time.

## Pattern in your examples

All 25 supplied codes have this shape:

```text
[A-Za-z0-9_-]{9}[AQgw]
```

They are canonical, unpadded Base64url encodings of seven bytes. The last character
can only be `A`, `Q`, `g`, or `w`; the generator now follows that format. This is
consistent with the examples, not proof of how Anthropic generates its codes.

There are `2^56 = 72,057,594,037,927,936` possible seven-byte values: 16 times fewer
possibilities than ten unrestricted characters. The examples do not reveal how
to predict an existing or unused code. At the default interval an eight-hour run
can start at most about 14,400 requests.

## Options and behavior

- `--workers 4`: up to four requests in flight (default 1, maximum 16).
- `--interval 2`: seconds between request starts globally (default 2 seconds).
- `--timeout 10`: request timeout in seconds.
- `--loop`: supplied links then generated codes, stopping on the first valid code.
- `--collect`: continue after valid results, including in loop/random-search mode.
- `--wait-on-rate-limit`: keep running but pause requests on HTTP 429, then retry.
- `--adaptive`: gradually increase speed after accepted checks, slow down on 429,
  and remember the learned minimum interval; includes rate-limit waiting.
- `--hours 8`: elapsed-time limit, including waiting and retries.
- `--state search.sqlite3`: persist progress and skip previously invalid codes.
  The loop, rate-limit waiting, and adaptive mode enable this automatically;
  other modes persist only when requested.
- `--output found_code.txt`: append valid URLs here.
- `--max-attempts 100`: bound generated codes without supplied links or `--loop`.

Concurrent checks retry temporary network failures and server errors twice with
backoff. Server-error retries also honor `Retry-After` (seconds or an HTTP date),
waiting for the longer of the server delay and local backoff. Missing or malformed
headers use local backoff. That wait applies to the affected check; HTTP 429 still
pauses all workers. Time limits and cancellation apply during retry waits.
HTTP 401/403 and detected browser challenges stop without retries.
HTTP 429 stops by default, or pauses requests with `--wait-on-rate-limit` or `--adaptive`.
When stopping, in-flight requests are cancelled and queued requests do not start.
An unexpected schema, redirect, or HTML response stops API checking. This is an
internal website endpoint and may change.

For legacy HTML inspection, `--match-text "TEXT FROM A KNOWN VALID PAGE"` switches
single-worker checks to page-text matching and saves unverified matches to
`candidate_links.txt`. It cannot be combined with the loop, collection,
concurrency, resume, adaptive pacing, or automatic rate-limit waiting.
Valid and invalid referral pages share the same generic initial page text.

Exit codes: `0` completed checks, reached the time limit, or found a valid link;
`1` unrecognized response; `2` input/request/output/state error; `130` Ctrl+C.
A zero exit status alone does not mean a valid code was found; inspect the output.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use simulated responses and make no requests to Claude. They cover strict
validity checks, code format, concurrent pacing, retries, early stopping,
cancellation, resume, file errors, and the original HTML checker.
Rate-limit tests cover Retry-After parsing, shared cooldowns, fixed local waits,
saved cooldowns across restarts, and time limits expiring while waiting.
Adaptive tests cover the speed cap, gradual increases, late responses, error
resets, shared cooldowns, and remembered minimum intervals across restarts.
# cl-tryy
