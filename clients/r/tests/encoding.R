# Plain-R tests: R CMD check runs every script in tests/.
library(bevis)
enc <- getFromNamespace("encode_rows", "bevis")
fmt <- getFromNamespace("format_decimal", "bevis")

df <- data.frame(
  year = c(2024L, 2025L),
  revenue = c(4210000, 3705000.5),
  segment = factor(c("SMB", NA)),
  active = c(TRUE, FALSE),
  day = as.Date(c("2025-03-01", NA)),
  stringsAsFactors = FALSE
)
cols <- bevis_evidence_columns(df)
stopifnot(identical(cols$type, c("integer", "decimal", "string", "boolean", "date")))

rows <- enc(df, cols)
stopifnot(identical(rows[[1]], list(2024L, "4210000", "SMB", TRUE, "2025-03-01")))
stopifnot(is.null(rows[[2]][[3]]), is.null(rows[[2]][[5]]))
stopifnot(identical(rows[[2]][[2]], "3705000.5"))

stopifnot(identical(fmt(c(0.1, 1e-7, 1/3, -0, 1e20)),
                    c("0.1", "0.0000001", "0.333333333333333", "0", "100000000000000000000")))
stopifnot(identical(fmt(c("12.50", NA)), c("12.50", NA)))
stopifnot(inherits(try(fmt("1,000"), silent = TRUE), "try-error"))

over <- bevis_evidence_columns(data.frame(k = "a", amount = "12.30"), types = c(amount = "decimal"))
stopifnot(identical(over$type, c("string", "decimal")))

# Keys and assumptions stay JSON arrays even with one element.
json <- jsonlite::toJSON(list(key = I("year"), assumptions = I(character())), auto_unbox = TRUE)
stopifnot(identical(as.character(json), '{"key":["year"],"assumptions":[]}'))

cat("encoding tests passed\n")
