#' Connect to a Bevis host
#'
#' @param url The host, e.g. "https://bevis.example.com". Defaults to the
#'   BEVIS_URL environment variable.
#' @param key An agent key or link. Defaults to BEVIS_KEY. Keep it out of
#'   scripts and version control.
#' @return A client to pass to the other functions.
#' @export
bevis_client <- function(url = Sys.getenv("BEVIS_URL", "http://localhost:8000"),
                         key = Sys.getenv("BEVIS_KEY")) {
  if (!nzchar(key)) stop("give `key`, or set BEVIS_KEY to an agent key", call. = FALSE)
  structure(list(url = sub("/+$", "", url), key = key), class = "bevis_client")
}

#' @export
print.bevis_client <- function(x, ...) {
  cat("<bevis_client>", x$url, "\n")
  invisible(x)
}

# json_verbatim: integers beyond 32 bits arrive as class "json" digit strings.
to_json <- function(body) {
  jsonlite::toJSON(body, auto_unbox = TRUE, null = "null", na = "null", digits = NA, json_verbatim = TRUE)
}

bevis_call <- function(client, method, path, body = NULL, query = list()) {
  req <- httr2::request(paste0(client$url, path))
  req <- httr2::req_method(req, method)
  req <- httr2::req_headers(req, Authorization = paste("Bearer", client$key), .redact = "Authorization")
  if (length(query)) req <- httr2::req_url_query(req, !!!query)
  if (!is.null(body)) {
    json <- to_json(body)
    req <- httr2::req_body_raw(req, json, type = "application/json")
  }
  req <- httr2::req_error(req, is_error = function(resp) FALSE)
  resp <- httr2::req_perform(req)
  status <- httr2::resp_status(resp)
  parsed <- tryCatch(httr2::resp_body_json(resp, simplifyVector = FALSE), error = function(e) NULL)
  if (status >= 400) {
    err <- parsed$error
    msg <- if (is.null(err)) httr2::resp_body_string(resp) else sprintf("%s: %s", err$code, err$message)
    cond <- structure(class = c("bevis_error", "error", "condition"),
                      list(message = sprintf("Bevis %d %s", status, msg), call = NULL, status = status,
                           code = err$code, details = err$details))
    stop(cond)
  }
  parsed
}

#' Submit a data frame as evidence
#'
#' The host versions the record: resubmitting the same `id` with changed
#' data creates a new version, and the same data is recorded as a re-run.
#'
#' @param client From [bevis_client()].
#' @param report_id The report, e.g. "rpt_4ddf4c186ae5".
#' @param id The evidence slug, e.g. "q-rev-yoy": 3 to 64 lowercase
#'   letters, digits, and hyphens, starting with a letter.
#' @param data A data frame of query results.
#' @param key Columns that identify a row. `character()` for a single row.
#' @param sql The query text, recorded for readers.
#' @param source Where the query ran, e.g. "warehouse.orders".
#' @param params Query parameters, recorded with the run.
#' @param assumptions Character vector of assumptions a reader should know.
#' @param types Named character vector overriding inferred column types.
#' @param partial,source_row_count For a sample or top-N of a larger result.
#' @param run_at When the query ran (ISO 8601). Defaults to now.
#' @param create_only Fail instead of adding a version if `id` exists.
#' @return The host's response: `id`, `version`, `outcome`, `flags`, and,
#'   for a new version, the `diff` from the previous one.
#' @export
bevis_add_evidence <- function(client, report_id, id, data, key, sql, source, params = list(),
                               assumptions = character(), types = NULL, partial = NULL,
                               source_row_count = NULL, run_at = NULL, create_only = FALSE) {
  stopifnot(is.data.frame(data))
  columns <- bevis_evidence_columns(data, types)
  body <- list(
    id = id, source = source, sql = sql,
    params = if (length(params)) params else structure(list(), names = character()),
    assumptions = I(as.character(assumptions)),
    key = I(as.character(key)),
    columns = lapply(seq_len(nrow(columns)), function(j) list(name = columns$name[j], type = columns$type[j])),
    rows = encode_rows(data, columns),
    run_at = if (is.null(run_at)) format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC") else run_at
  )
  if (!is.null(partial)) body$partial <- partial
  if (!is.null(source_row_count)) body$source_row_count <- source_row_count
  if (isTRUE(create_only)) body$create_only <- TRUE
  bevis_call(client, "POST", sprintf("/api/reports/%s/evidence", report_id), body)
}

#' Run a query with DBI and submit the result as evidence
#'
#' @inheritParams bevis_add_evidence
#' @param con A DBI connection.
#' @param params Passed to [DBI::dbGetQuery()] and recorded with the run.
#' @param ... Further arguments to [bevis_add_evidence()].
#' @export
bevis_run_query <- function(client, report_id, id, con, sql, key, source, params = list(), ...) {
  if (!requireNamespace("DBI", quietly = TRUE)) stop("bevis_run_query needs the DBI package", call. = FALSE)
  run_at <- format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
  data <- if (length(params)) DBI::dbGetQuery(con, sql, params = params) else DBI::dbGetQuery(con, sql)
  recorded <- if (is.null(names(params)) && length(params)) list(args = unname(params)) else params
  bevis_add_evidence(client, report_id, id, data, key = key, sql = sql, source = source,
                     params = recorded, run_at = run_at, ...)
}

#' Create a report
#'
#' @param title The report's title.
#' @param client From [bevis_client()].
#' @param owner Name of the person who will own it. The response holds a
#'   one-time admin link for them.
#' @export
bevis_create_report <- function(client, title, owner = NULL) {
  bevis_call(client, "POST", "/api/reports", list(title = title, owner = owner))
}

#' Read a report's draft, or a published version
#'
#' @param client From [bevis_client()].
#' @param report_id The report.
#' @param version A published version number; the draft when `NULL`.
#' @export
bevis_get_report <- function(client, report_id, version = NULL) {
  bevis_call(client, "GET", sprintf("/api/reports/%s", report_id),
             query = if (is.null(version)) list() else list(version = version))
}

#' Apply edit operations to a draft (editors)
#'
#' @param client From [bevis_client()].
#' @param report_id The report.
#' @param base_revision Fail if the draft has moved past this revision.
#' @param ops A list of operations, e.g.
#'   `list(list(op = "set_title", title = "New title"))`.
#' @export
bevis_edit <- function(client, report_id, ops, base_revision = NULL) {
  bevis_call(client, "POST", sprintf("/api/reports/%s/edits", report_id),
             list(ops = ops, base_revision = base_revision))
}

#' Propose edit operations as a suggestion for an editor to accept (suggesters)
#'
#' @param client From [bevis_client()].
#' @param report_id The report.
#' @param ops A list of operations, as for [bevis_edit()].
#' @param message A message for the reviewer.
#' @param base_revision Fail if the draft has moved past this revision.
#' @export
bevis_suggest <- function(client, report_id, ops, message = "", base_revision = NULL) {
  bevis_call(client, "POST", sprintf("/api/reports/%s/suggestions", report_id),
             list(ops = ops, message = message, base_revision = base_revision))
}

#' Publish the draft as a version (editors)
#'
#' @param client From [bevis_client()].
#' @param report_id The report.
#' @param override_reason Publish despite validation errors; recorded on the version.
#' @export
bevis_publish <- function(client, report_id, override_reason = NULL) {
  bevis_call(client, "POST", sprintf("/api/reports/%s/publish", report_id),
             list(override_reason = override_reason))
}

#' Open challenges readers have filed
#'
#' @param client From [bevis_client()].
#' @param report_id One report, or every report the key can see when `NULL`.
#' @export
bevis_list_open_challenges <- function(client, report_id = NULL) {
  bevis_call(client, "GET", "/api/challenges/open",
             query = if (is.null(report_id)) list() else list(report_id = report_id))
}

#' Answer a challenge, optionally proposing an edit as a suggestion
#'
#' @param client From [bevis_client()].
#' @param report_id The report.
#' @param challenge_id The challenge.
#' @param body The answer.
#' @param ops Edit operations to propose; they become a suggestion for an editor.
#' @param message A message for the suggestion.
#' @export
bevis_reply_to_challenge <- function(client, report_id, challenge_id, body, ops = NULL, message = "") {
  bevis_call(client, "POST", sprintf("/api/reports/%s/challenges/%s/replies", report_id, challenge_id),
             list(body = body, ops = ops, message = message))
}
