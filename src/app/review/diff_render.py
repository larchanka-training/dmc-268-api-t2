"""Text rendering of FileDiff shared by chunk sizing and the prompt.

Each line carries its old/new numbers so the model can cite exact coordinates.
"""

from app.review.schemas.context import FileDiff, Hunk, LineKind

_MARKERS = {LineKind.ADDED: "+", LineKind.DELETED: "-", LineKind.CONTEXT: " "}


def render_file_header(diff: FileDiff) -> str:
    if diff.status == "renamed":
        return f"FILE {diff.old_path} -> {diff.new_path} (renamed)"
    return f"FILE {diff.new_path or diff.old_path} ({diff.status})"


def render_hunk(hunk: Hunk) -> str:
    header = (
        f"@@ -{hunk.old_start},{hunk.old_count} "
        f"+{hunk.new_start},{hunk.new_count} @@ {hunk.hunk_id}"
    )
    lines = [
        f"{line.old_line or '':>5} {line.new_line or '':>5} "
        f"{_MARKERS[line.kind]} {line.text}"
        for line in hunk.lines
    ]
    return "\n".join([header, *lines])


def render_file_diff(diff: FileDiff) -> str:
    return "\n".join([render_file_header(diff), *map(render_hunk, diff.hunks)])
