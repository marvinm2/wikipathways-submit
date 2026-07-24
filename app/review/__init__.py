# Kept import-free to avoid an import cycle: app.models imports app.review.checklist,
# and app.review.service imports app.models. Import submodules directly
# (app.review.service, app.review.checklist).
