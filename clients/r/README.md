# bevis (R client)

Submit query results to a Bevis host as evidence, from R.

```r
# install.packages("remotes"); remotes::install_github("natefduncan/bevis", subdir = "clients/r")
library(bevis)
client <- bevis_client()  # BEVIS_URL and BEVIS_KEY from the environment

con <- DBI::dbConnect(...)
bevis_run_query(client, "rpt_…", "q-rev-yoy", con,
  "SELECT year, SUM(amount) AS revenue FROM orders WHERE refunded = false GROUP BY year",
  key = "year", source = "warehouse.orders",
  assumptions = "revenue = SUM(amount), refunds excluded")
```

Column types are read from the data frame: integer, double (as decimal),
character and factor (as string), logical, Date, POSIXct, and
`bit64::integer64` (as exact integers). Time-of-day columns (`hms`, `difftime`)
have no evidence type: declare them, e.g. `types = c(t = "string")`. Doubles
are sent as plain decimals with 15 significant digits, whatever
`options(OutDec)` says. A POSIXct column declared `date` keeps the date in its
own time zone. A column declared `boolean` holds logicals or the text
`"true"`/`"false"` (any case); other values are an error. For exact money values, cast to text in SQL
and declare the column: `types = c(revenue = "decimal")`. A double column
declared `integer` (`types = c(n = "integer")`) is sent as exact whole
numbers, also beyond R's 32-bit integer range.

Tests: `R CMD check` runs `tests/encoding.R`, and `tests/host.R` when
`BEVIS_TEST_URL` and `BEVIS_TEST_KEY` point at a running host.
