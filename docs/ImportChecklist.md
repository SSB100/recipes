# Recipe import review checklist

## Before importing

- Confirm the supplied files may be used for the intended purpose. Public accessibility alone is not permission for systematic collection or republication.
- Record a nonsecret rights reference. Keep actual contracts private.
- Use `internal-sample` only for an explicitly limited factual evaluation; it does not establish production rights.
- Keep source exports outside the public repository. Use a fresh local output directory.

## After importing

- Read the CLI counts and exit code. Inspect `errors.json` and each recipe's validation warnings.
- Confirm expected record and ingredient counts. Review conflicting duplicate URLs.
- Verify prep, cook and total are labeled source values; do not derive cook time by subtracting prep from total.
- Check exact cooking quantities against original ingredient text, including “to taste”, bunches, blocks and prepared-stock volumes.
- Review canonical ingredient forms independently: cooked versus dry rice, dry stock versus prepared stock, and blocks or bunches versus measured weights are different quantities. Canonical labels are local normalizations, not matches to a Pantri master list.
- Confirm diet labels and nutrition are explicitly observed; do not use unverified labels as allergen assurances.
- Check capture dates and source freshness before relying on rating totals or recipe details.
- Keep unavailable prose blank. Only explicitly authorized supplied prose may use the text-import option.
- Verify `--resume` before distributing a completed batch, and give recipients the relevant source/rights limitations.

## Later Pantri integration

1. Confirm suitable permission, then obtain an authorized export or run a reviewed public-page collection pilot. Define any refresh agreement.
2. Import a small batch, review rejected rows and compare source facts.
3. Approve normalized fields and licensed content for the intended use.
4. Match approved ingredient labels to the actual Pantri master list in a separately reviewed stage. Preserve source names and unresolved forms.
5. Connect approved data to Pantri through a separately scoped, tested production change. This package does not deploy anything.
