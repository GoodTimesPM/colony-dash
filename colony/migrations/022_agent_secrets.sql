-- A contract can now say whether the agent's checkout gets the credential
-- files copied into it. Off for every existing agent, and off for every new
-- one unless the PO ticks it.
--
-- Why a flag and not a rule: an agent asked to check that the OG-tracker sync
-- reads NOTION_API_KEY cannot answer without seeing whether the key is there,
-- and "it is not in the checkout" is not the same answer as "it is not set".
-- An agent asked to rename a variable has no business with the file. The PO
-- decides which of the two he is hiring.
--
-- The credentials never reach git. `worktree.seed` copies them in after the
-- base tree is written and `worktree.diff` deletes them before it looks, so
-- no patch can carry a key even if the agent edits the file.
--
-- A plain ALTER: adding a nullable column with a default touches no CHECK and
-- no index, so none of the table-rebuild work 020 and 021 needed applies here.

ALTER TABLE agents ADD COLUMN sees_secrets INTEGER NOT NULL DEFAULT 0;
