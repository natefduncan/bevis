# Against a running host. Skipped unless BEVIS_TEST_URL and BEVIS_TEST_KEY are set.
url <- Sys.getenv("BEVIS_TEST_URL")
key <- Sys.getenv("BEVIS_TEST_KEY")
if (!nzchar(url) || !nzchar(key)) {
  cat("host tests skipped (set BEVIS_TEST_URL and BEVIS_TEST_KEY)\n")
} else {
  library(bevis)
  client <- bevis_client(url, key)
  rid <- bevis_create_report(client, "From R")$id

  df <- data.frame(year = c(2024L, 2025L), revenue = c(4210000, 3705000))
  out <- bevis_add_evidence(client, rid, "q-rev-yoy", df, key = "year",
                            sql = "SELECT year, SUM(amount) AS revenue FROM orders GROUP BY year",
                            source = "warehouse.orders", assumptions = "refunds excluded")
  stopifnot(out$outcome == "created", out$version == 1L, out$row_count == 2L)
  again <- bevis_add_evidence(client, rid, "q-rev-yoy", df, key = "year", sql = "SELECT 1", source = "x")
  stopifnot(again$outcome == "same_result", "query text changed" %in% unlist(again$flags))

  single <- bevis_add_evidence(client, rid, "q-total", data.frame(total = 7915000), key = character(),
                               sql = "SELECT SUM(amount)", source = "warehouse.orders")
  stopifnot(single$outcome == "created")

  edit <- bevis_edit(client, rid, list(
    list(op = "upsert_block", block = list(type = "text", text = "[Revenue fell 12.0%]{#c1}.")),
    list(op = "link_claim", claim = list(
      id = "c1", block_id = "b1", evidence = list(list(id = "q-rev-yoy", version = 1L)),
      numbers = list(list(text = "12.0%", tier = "computed",
        expression = "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))"))))))
  stopifnot(edit$validation$counts$errors == 0L)
  title <- list(list(op = "set_title", title = "From R, renamed"))
  stale <- tryCatch(bevis_suggest(client, rid, title, base_revision = edit$revision - 1L),
                    bevis_error = function(e) e)
  stopifnot(inherits(stale, "bevis_error"), stale$status == 409L, stale$code == "stale_revision")
  sug <- bevis_suggest(client, rid, title, "rename", base_revision = edit$revision)
  stopifnot(sug$status == "open", sug$revision == edit$revision)
  stopifnot(bevis_publish(client, rid)$version == 1L)

  err <- tryCatch(bevis_get_report(client, "rpt_missing"), bevis_error = function(e) e)
  stopifnot(inherits(err, "bevis_error"), err$status == 404L, err$code == "not_found")

  big <- data.frame(id = 1:3, n = c(3e9, -3e9, 2^53 + 2))
  big_out <- bevis_add_evidence(client, rid, "q-big", big, key = "id", sql = "SELECT id, n", source = "x",
                                types = c(n = "integer"))
  stopifnot(big_out$outcome == "created")
  call <- getFromNamespace("bevis_call", "bevis")
  stored <- call(client, "GET", paste0("/api/reports/", rid, "/evidence/q-big"))
  stopifnot(identical(vapply(stored$evidence$rows, function(r) r[[2]], 0), big$n))

  bad <- tryCatch(bevis_add_evidence(client, rid, "Bad Slug", df, key = "year", sql = "x", source = "x"),
                  bevis_error = function(e) e)
  stopifnot(bad$status == 422L)
  cat("host tests passed\n")
}
