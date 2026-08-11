# Handover Prompt For Claude

You are taking over a spreadsheet automation project for restaurant recipe consumption reporting.

## Project Goal

Build and refine a weekly system that takes:

1. `Recipe builder.xlsx`
2. weekly item-wise sales report
3. weekly option-wise sales report

and produces an Excel output showing raw material consumption by brand and by store/site.

The business problem is:

Sales reports contain sold items and options. Recipe Builder contains recipes/ingredients. We need to map sold items/options to recipes and calculate:

`sold quantity x recipe quantity = raw material used`

The tricky part is that one single matching logic does not work for all items. Burgers, wings, tenders, meal deals, sauces, and bundles behave differently.

## Current Folder

Project root:

`C:\Users\TEJAS\Documents\Codex\2026-05-20\files-mentioned-by-the-user-recipe`

## Important Files

### Inputs

Use these as current sample inputs:

`inputs\Recipe builder.xlsx`

`inputs\Items-wise-order-transactions-19711669-2026-05-05.xlsx`

`inputs\Options-wise-order-transactions-19711669-2026-05-05.xlsx`

### Existing Scripts

`build_recipe_usage_data.py`

Computes recipe usage data from the input workbooks and writes:

`outputs\recipe_usage_system\recipe_usage_data.json`

`build_recipe_usage_workbook.mjs`

Builds the final Excel output workbook from the JSON data.

`run_recipe_usage_system.ps1`

Weekly runner. It runs the Python data build and the JS workbook build.

`verify_recipe_usage_workbook.mjs`

Imports and checks the output workbook, renders previews, and scans for formula errors.

`build_proposed_plu_mapping.py`

Creates a proposed rule-based PLU mapping from the current Recipe Builder and sales reports.

`build_proposed_plu_mapping_workbook.mjs`

Exports the proposed PLU mapping to Excel.

### Documentation

`README - Weekly Recipe Usage System.txt`

Current simple operating instructions.

### Outputs

`outputs\recipe_usage_system\Raw Material Usage System - 2026-05-05.xlsx`

Latest raw-material usage output.

`outputs\recipe_usage_system\Proposed PLU Mapping - Rule Based.xlsx`

Important: this is the proposed new mapping structure with `Rule_Type`.

## Current Understanding Of The Logic

The system should not use one universal rule for all items.

The correct design is to use a `Rule_Type` in PLU Mapping.

Recommended rule types:

### ITEM_ONLY

Use itemised report quantity and `Item ref ID`.

Example:

`OG Burger` sold directly.

Mapping:

`Recipe Item = OG Burger`

`Rule_Type = ITEM_ONLY`

`Item_id = HCCBPOG01`

`Option_id_1 = blank`

Quantity source:

`itemised report Quantity`

### OPTION_COMPONENT

Use optionwise report quantity and `Option Ref ID`.

Used when a component is selected inside a meal deal/bundle.

Example:

`OG Burger` selected inside `Hungry as Cluck Feast`.

Mapping:

`Recipe Item = OG Burger`

`Rule_Type = OPTION_COMPONENT`

`Item_id = blank`

`Option_id_1 = HCCBMOG01`

Quantity source:

`optionwise report Option Quantity`

This is important for burgers. The previous mapping sometimes had both `Item_id` and `Option_id` in one row, but for burgers that should usually be split into two independent rows:

1. direct item sale from itemwise report
2. meal-deal component from optionwise report

### ITEM_OPTION_COMBO

Use itemised report quantity, but only when an itemised line has both the item code and required option code attached to the same parent `unique id`.

Used for configurable products where the option selects the exact recipe.

Example:

`Naked Wings` + `8 Wings`

Mapping:

`Recipe Item = Naked Wings - 8`

`Rule_Type = ITEM_OPTION_COMBO`

`Item_id = HCWGPNK01`

`Option_id_1 = HCWGMSZ08`

Quantity source:

`itemised report Quantity`

Important:

The `8 Wings` option is a recipe selector. Do not count it again separately from the optionwise report.

### ITEM_MULTI_OPTION_COMBO

Same idea, but exact recipe needs multiple options.

Example:

`Naked Wings` + `8 Wings` + `Sriracha Sauce`

Mapping:

`Recipe Item = Naked Wings - 8 - Sriracha`

`Rule_Type = ITEM_MULTI_OPTION_COMBO`

`Item_id = HCWGPNK01`

`Option_id_1 = HCWGMSZ08`

`Option_id_2 = HCWGMSR01`

Quantity source:

`itemised report Quantity`

Both option IDs are selectors and should not be counted again separately.

### OPTION_OPTION_COMBO

Use optionwise report when a recipe component inside a meal deal is itself defined by multiple option rows under the same parent unique id.

Example pattern:

Meal deal option 1 = wings flavour

Meal deal option 2 = wings size

Together they define the exact wings recipe.

### STANDALONE_ADDON

Use optionwise report quantity.

Used for true paid extras/add-ons/dips that are separate consumption.

Example:

`Extra Ranch Dip`

Mapping:

`Rule_Type = STANDALONE_ADDON`

`Item_id = blank`

`Option_id_1 = HCWGMRC01`

Quantity source:

`optionwise report Option Quantity`

## Key Report Relationships

The itemised report has:

`Item ref ID`

`Quantity`

`options`

`option_ids`

`unique id`

But the itemised `option_ids` are numeric platform IDs, not PLU codes.

The optionwise report has:

`Option ID` = numeric platform ID

`Option Ref ID` = actual PLU code

`Option Title`

`Option Quantity`

`unique id`

To connect itemised and optionwise rows:

Use itemised `unique id` as the parent.

Optionwise `unique id` usually starts with the parent and adds one final suffix.

Example:

Itemised parent:

`C-1117221218-1`

Optionwise children:

`C-1117221218-1-1`

`C-1117221218-1-2`

Therefore, do not group only by `Order ID`. Use the parent line identity.

## Known Issue / Reason For Latest Change

The current developed logic was too combo-heavy for some products.

For wings, combo logic is correct:

`Naked Wings + 8 Wings = Naked Wings - 8`

But for burgers, many rows should not be combo.

Example current old PLU Mapping style:

`OG Burger | Item_id = HCCBPOG01 | Option_id = HCCBMOG01`

This should be split:

`OG Burger | ITEM_ONLY | Item_id = HCCBPOG01`

`OG Burger | OPTION_COMPONENT | Option_id_1 = HCCBMOG01`

Because direct burger sales are in itemwise report, and burger selections inside meal deals are in optionwise report.

## Proposed PLU Mapping Workbook

The generated workbook:

`outputs\recipe_usage_system\Proposed PLU Mapping - Rule Based.xlsx`

has:

1. `Proposed PLU Mapping`
2. `Rule Definitions`
3. `Summary`

This workbook should be reviewed before becoming the master PLU Mapping.

Important columns:

`Brand`

`Category`

`Recipe Item`

`Rule_Type`

`Item_id`

`Option_id_1`

`Option_id_2`

`Option_id_3`

`Quantity_Source`

`Source_Row_Type`

`Confidence`

`Notes`

Rows marked `High` confidence are generally inferred from clear current sales patterns.

Rows marked `Review` or `Low` need manual confirmation.

Important: some duplicate code keys exist in the source data. For example, some burger rows map the same code to multiple recipe names. These must be cleaned manually.

## What I Need You To Do Next

1. Review the current scripts and the proposed mapping workbook.

2. Update `build_recipe_usage_data.py` so it uses the new rule-based PLU mapping, not only the old combo/direct logic.

3. The matching priority should be:

   - `ITEM_MULTI_OPTION_COMBO` / `ITEM_OPTION_COMBO`
   - `ITEM_ONLY`
   - `OPTION_OPTION_COMBO`
   - `OPTION_COMPONENT`
   - `STANDALONE_ADDON`

4. Any option used as a combo selector must be marked as used and not counted again.

5. Any leftover option should be counted only if it has `OPTION_COMPONENT` or `STANDALONE_ADDON`.

6. Keep a detailed audit sheet in the output:

   - parent unique id
   - item ref ID
   - option ref IDs found
   - rule type used
   - recipe matched
   - quantity source
   - whether each option was used, counted, ignored, or unmatched

7. Produce a clean final output workbook.

8. Preserve the weekly usage flow:

   Put new files in `inputs`.

   Run `run_recipe_usage_system.ps1`.

   Output should go to `outputs\recipe_usage_system`.

## Files To Ask Me For / Use

Required:

- `inputs\Recipe builder.xlsx`
- `inputs\Items-wise-order-transactions-19711669-2026-05-05.xlsx`
- `inputs\Options-wise-order-transactions-19711669-2026-05-05.xlsx`
- `build_recipe_usage_data.py`
- `build_recipe_usage_workbook.mjs`
- `run_recipe_usage_system.ps1`
- `build_proposed_plu_mapping.py`
- `build_proposed_plu_mapping_workbook.mjs`
- `README - Weekly Recipe Usage System.txt`
- `outputs\recipe_usage_system\Proposed PLU Mapping - Rule Based.xlsx`
- `outputs\recipe_usage_system\Raw Material Usage System - 2026-05-05.xlsx`

Not required:

- `node_modules`
- preview PNG files
- old JSON intermediates unless needed for debugging

## Caution

Do not assume all rows with both `Item_id` and `Option_id` are combos.

That is the main business logic trap.

Use `Rule_Type`.

Different product families need different logic.

