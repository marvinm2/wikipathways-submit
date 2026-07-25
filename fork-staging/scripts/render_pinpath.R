#!/usr/bin/env Rscript
# Render a GPML file to SVG with PinPath (https://github.com/SyNUM-lab/PinPath) for the
# before/after pathway viewer (wikipathways-submit issue #11).
#
# Usage: Rscript render_pinpath.R <input.gpml> <outdir> <outname.svg>
#
# A *plain* render: every data-overlay argument of PinPath::drawGPML() is optional, so no
# Bioconductor annotation package (org.Hs.eg.db etc.) is needed — just PinPath + its imports.
# drawGPML writes <outdir>/<outname>; we only care about the pathway image (legend disabled).

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("usage: render_pinpath.R <input.gpml> <outdir> <outname.svg>")
}
infile <- args[[1]]
outdir <- args[[2]]
outname <- args[[3]]

suppressMessages(library(PinPath))

dir.create(outdir, showWarnings = FALSE, recursive = TRUE)

PinPath::drawGPML(
  infile = infile,
  outdir = outdir,
  outname = outname,
  legend = FALSE,
  nodeTable = FALSE,
  pathInfo = FALSE,
  openFile = FALSE
)

if (!file.exists(file.path(outdir, outname))) {
  stop(sprintf("PinPath did not produce %s", file.path(outdir, outname)))
}
