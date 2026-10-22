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

# Integral doubles beyond 32-bit range go out as exact JSON numbers, not null.
to_json <- getFromNamespace("to_json", "bevis")
big <- data.frame(n = c(3e9, -3e9, 2^53 + 2, 1e20, 7, NA))
big_cols <- bevis_evidence_columns(big, types = c(n = "integer"))
stopifnot(identical(as.character(to_json(enc(big, big_cols))),
                    "[[3000000000],[-3000000000],[9007199254740994],[100000000000000000000],[7],[null]]"))
stopifnot(identical(enc(big, big_cols)[[5]], list(7L)))
stopifnot(inherits(try(enc(data.frame(n = 2.5e9 + 0.5), big_cols), silent = TRUE), "try-error"))
stopifnot(inherits(try(enc(data.frame(n = Inf), big_cols), silent = TRUE), "try-error"))
stopifnot(inherits(try(enc(data.frame(n = "3000000000"), big_cols), silent = TRUE), "try-error"))

# Decimals: 15 significant digits in plain notation, whatever the locale.
stopifnot(identical(fmt(c(123456789012345678, -2.5e-7, 1e-320)),
                    c("123456789012346000", "-0.00000025",
                      paste0("0.", strrep("0", 320), "999988867182683"))))
old <- options(OutDec = ",")
stopifnot(identical(fmt(0.5), "0.5"))
stopifnot(identical(enc(data.frame(x = 1.25), bevis_evidence_columns(data.frame(x = 1.25)))[[1]][[1]], "1.25"))
options(old)

# A POSIXct column declared date keeps the date in its own time zone.
ny <- data.frame(day = as.POSIXct("2025-03-01 23:30:00", tz = "America/New_York"))
stopifnot(identical(enc(ny, bevis_evidence_columns(ny, types = c(day = "date")))[[1]][[1]], "2025-03-01"))

# Booleans: logical, or the text "true"/"false" in any case. Anything else is an error, not null.
flags <- data.frame(b = c("TRUE", "false", NA), stringsAsFactors = FALSE)
flag_cols <- bevis_evidence_columns(flags, types = c(b = "boolean"))
stopifnot(identical(enc(flags, flag_cols), list(list(TRUE), list(FALSE), list(NULL))))
stopifnot(inherits(try(enc(data.frame(b = "f"), flag_cols), silent = TRUE), "try-error"))
stopifnot(inherits(try(enc(data.frame(b = 2), flag_cols), silent = TRUE), "try-error"))

if (requireNamespace("bit64", quietly = TRUE)) {
  ids <- data.frame(id = bit64::as.integer64(c("9007199254740993", NA, "-5")))
  id_cols <- bevis_evidence_columns(ids)
  stopifnot(identical(id_cols$type, "integer"))
  stopifnot(identical(as.character(to_json(enc(ids, id_cols))), "[[9007199254740993],[null],[-5]]"))
}
if (requireNamespace("hms", quietly = TRUE)) {
  at <- data.frame(t = hms::as_hms(c(3600, 5400)))
  stopifnot(inherits(try(bevis_evidence_columns(at), silent = TRUE), "try-error"))
  stopifnot(identical(enc(at, bevis_evidence_columns(at, types = c(t = "string")))[[1]][[1]], "01:00:00"))
}

cat("encoding tests passed\n")
