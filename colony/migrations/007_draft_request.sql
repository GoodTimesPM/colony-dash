-- The PO asking for a draft is not the same as the draft existing.
--
-- `skills.status` is a four-value enum from 001 (candidate / drafted / active /
-- retired) and widening it would mean rebuilding the table to change a CHECK.
-- The request is a timestamp instead, which is the more useful shape anyway:
-- "asked for at 14:02, still not drafted" is a question the dashboard can
-- answer, and a boolean could not.
--
-- Why the request is queued at all rather than drafted on the click: control.py
-- does not spend tokens (its module docstring is the rule). The gate and the
-- spender stay separate, so a mis-click costs nothing — the next wake picks the
-- request up, and the budget guard still gets its say first.

ALTER TABLE skills ADD COLUMN draft_requested_at TEXT;
