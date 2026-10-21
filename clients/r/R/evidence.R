# Typing a data frame for add_evidence (spec/bundle/README.md, "Value encoding").

infer_type <- function(x, name) {
  if (is.factor(x)) return("string")
  if (inherits(x, "POSIXt")) return("datetime")
  if (inherits(x, "Date")) return("date")
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
    out[ok] <- vapply(x[ok], function(v) format(v, digits = 15, scientific = FALSE, trim = TRUE), "")
  }
  out
}

encode_column <- function(x, type, name) {
  switch(type,
    integer = {
      if (is.double(x) && any(x[!is.na(x)] != round(x[!is.na(x)]))) {
        stop(sprintf("column '%s' is declared integer but holds fractions", name), call. = FALSE)
      }
      as.integer(x)
    },
    decimal = format_decimal(x),
    string = as.character(x),
    boolean = as.logical(x),
    date = ifelse(is.na(x), NA_character_, format(as.Date(x), "%Y-%m-%d")),
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
