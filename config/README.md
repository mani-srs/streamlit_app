# config/

Editable without code changes. Read once per process (restart the app after editing).

## models.yaml

Role → Cortex model. Used only via `core/llm.py`.

```yaml
roles:
  translator:            # cd_codegen AI slots
    model: claude-4-sonnet
    fallback: [llama3.1-70b]
    max_tokens: 6000
    temperature: 0
```

Roles: `planner`, `evaluator` (md_plan), `translator` (cd_codegen). Each model is tried twice before
the next fallback. The model must be available to your account/region (or via cross-region inference).
If the file or PyYAML is missing, defaults in `core/models.py` apply.

## medallion_rules.yaml

Layer assignment for the classifier.

- `precedence`: order of checks – `name` (regex on object name), `graph` (sources → bronze, terminal views/dynamic tables → gold), `schema` (regex on schema name)
- `default`: layer when nothing matches
- `layers.<bronze|silver|gold>.name_patterns` / `schema_patterns`: case-insensitive regexes

Reviewer overrides at the classifier gate (`layer: DB.SCHEMA.OBJECT = gold`) always win.

## pricing.yaml

Cortex COMPLETE rates for **estimated** cost, in AI credits per 1M tokens, separately for input and output:

```yaml
models:
  claude-4-sonnet: {input_per_m: 1.8, output_per_m: 9.0, source: "fitted to this account's billing"}
  llama3.1-70b:    {input_per_m: null, output_per_m: null, source: "not calibrated"}
```

A model without rates shows as "unknown" in estimates. **Financial → Billing → Calibrate rates from billing** fits
the rates from your billed calls and saves them as overrides in `.state/settings.json` (together with the price per
credit and the monthly budget). Billed credits themselves always come from Snowflake, not from this file.
