# Typing a data frame for add_evidence (spec/bundle/README.md, "Value encoding").

infer_type <- function(x, name) {
  if (is.factor(x)) return("string")
  if (inherits(x, "POSIXt")) return("datetime")
  if (inherits(x, "Date")) return("date")
  if (inherits(x, "integer64")) return("integer")
  if (inherits(x, "difftime")) {
    stop(sprintf("column '%s' holds times (%s), which have no evidence type; give its type in `types`, e.g. \"string\"",
                 name, paste(class(x), collapse = "/")), call. = FALSE)
  }
  if (is.logical(x)) return(if (all(is.na(x))) "null" else "boolean")
  if (is.integer(x)) return("integer")
  if (is.double(x)) return("decimal")
  if (is.character(x)) return("string")
  stop(sprintf("column '%s' has class %s, which has no evidence type; convert it, or give its type in `types`",
               name, paste(class(x), collapse = "/")), call. = FALSE)
}

# Doubles are written in fixed notation with 15 significant digits: as exact as a
# double allows. For money and other exact values, cast to text in SQL and declare
# the column "decimal" in `types`; the text then passes through unchanged.
format_decimal <- function(x) {
  out <- rep(NA_character_, length(x))
  ok <- !is.na(x)
  if (is.numeric(x) && any(!is.finite(x[ok]))) stop("a decimal column holds Inf or NaN", call. = FALSE)
  if (is.character(x)) {
    bad <- ok & !grepl("^-?[0-9]+(\\.[0-9]+)?$", x)
    if (any(bad)) stop(sprintf("not a decimal: '%s'", x[bad][1]), call. = FALSE)
    out[ok] <- x[ok]
  } else {
    out[ok] <- if (inherits(x, "integer64")) as.character(x[ok]) else vapply(as.double(x[ok]), plain_decimal, "")
  }
  out
}

# A double as plain decimal text with 15 significant digits. sprintf reads the
# digits exactly (format() can give more, and exponents for subnormals) and is
# not affected by options(OutDec).
plain_decimal <- function(v) {
  if (v == 0) return("0")
  parts <- strsplit(sprintf("%.14e", abs(v)), "e", fixed = TRUE)[[1]]
  digits <- sub("0+$", "", sub(".", "", parts[1], fixed = TRUE))
  point <- as.integer(parts[2]) + 1L
  n <- nchar(digits)
  out <- if (point <= 0) {
    paste0("0.", strrep("0", -point), digits)
  } else if (point >= n) {
    paste0(digits, strrep("0", point - n))
  } else {
    paste0(substr(digits, 1, point), ".", substr(digits, point + 1, n))
  }
  if (v < 0) paste0("-", out) else out
}

# Accepts logicals and the text "true"/"false" in any case; anything else would
# otherwise become NA (null) or be reinterpreted, so it is an error.
encode_boolean <- function(x, name) {
  if (is.logical(x)) return(x)
  if (is.factor(x)) x <- as.character(x)
  if (!is.character(x)) {
    stop(sprintf("column '%s' is declared boolean but holds %s values", name, paste(class(x), collapse = "/")),
         call. = FALSE)
  }
  out <- c(true = TRUE, false = FALSE)[tolower(x)]
  bad <- !is.na(x) & is.na(out)
  if (any(bad)) stop(sprintf("column '%s' is declared boolean but holds '%s'", name, x[bad][1]), call. = FALSE)
  unname(out)
}

# bit64::integer64 columns (bigint from RPostgres) keep their exact digits.
encode_integer64 <- function(x) {
  out <- as.list(rep(NA, length(x)))
  ok <- !is.na(x)
  out[ok] <- lapply(as.character(x[ok]), function(s) structure(s, class = "json"))
  out
}

# R integers stop at 32 bits. Integral doubles outside that range are written as
# their exact digits and sent verbatim as JSON numbers (see to_json).
encode_integral_double <- function(x, name) {
  ok <- !is.na(x)
  if (any(!is.finite(x[ok]))) stop(sprintf("column '%s' is declared integer but holds Inf", name), call. = FALSE)
  if (any(x[ok] != round(x[ok]))) {
    stop(sprintf("column '%s' is declared integer but holds fractions", name), call. = FALSE)
  }
  big <- ok & abs(x) > .Machine$integer.max
  if (!any(big)) return(as.integer(x))
  out <- as.list(as.integer(ifelse(big, NA, x)))
  out[big] <- lapply(sprintf("%.0f", x[big]), function(s) structure(s, class = "json"))
  out
}

encode_column <- function(x, type, name) {
  switch(type,
    integer = {
      if (inherits(x, "integer64")) encode_integer64(x)
      else if (is.double(x)) encode_integral_double(x, name) else {
        # as.integer warns and gives NA when it can't convert; never send that as null.
        withCallingHandlers(as.integer(x), warning = function(w) {
          stop(sprintf("column '%s' is declared integer: %s", name, conditionMessage(w)), call. = FALSE)
        })
      }
    },
    decimal = format_decimal(x),
    string = as.character(x),
    boolean = encode_boolean(x, name),
    # A POSIXct is formatted in its own time zone, not cut to its UTC date.
    date = ifelse(is.na(x), NA_character_, format(if (inherits(x, "POSIXt")) x else as.Date(x), "%Y-%m-%d")),
    datetime = {
      if (is.character(x)) x else ifelse(is.na(x), NA_character_,
                                         format(as.POSIXct(x), "%Y-%m-%dT%H:%M:%OS6Z", tz = "UTC"))
    },
    null = rep(NA, length(x)),
    stop(sprintf("unknown type '%s' for column '%s'", type, name), call. = FALSE)
  )
}

#' Column types for a data frame, as add_evidence sends them
#'
#' @param data A data frame.
#' @param types Named character vector overriding inferred types,
#'   e.g. `c(amount = "decimal")`.
#' @return A data frame with `name` and `type`.
#' @export
bevis_evidence_columns <- function(data, types = NULL) {
  names <- names(data)
  type <- vapply(names, function(n) {
    if (!is.null(types) && n %in% names(types)) types[[n]] else infer_type(data[[n]], n)
  }, "")
  data.frame(name = names, type = unname(type), stringsAsFactors = FALSE)
}

encode_rows <- function(data, columns) {
  cols <- lapply(seq_len(nrow(columns)), function(j) {
    encode_column(data[[columns$name[j]]], columns$type[j], columns$name[j])
  })
  # Row-major list of lists; NA becomes JSON null.
  lapply(seq_len(nrow(data)), function(i) {
    lapply(cols, function(col) {
      v <- col[[i]]
      if (is.na(v)) NULL else v
    })
  })
}
