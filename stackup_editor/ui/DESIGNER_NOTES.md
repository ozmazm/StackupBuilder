Open [stackup_editor_main.ui](/C:/Users/donme/OneDrive/Belgeler/StackUpEditor%202/stackup_editor/ui/stackup_editor_main.ui) in `Qt Designer` or `Qt Creator`.

You can safely change:

- Layouts, spacings, margins, splitter arrangement
- Button texts
- Group titles
- Label texts
- Pane sizes and widget positions

Keep these `objectName` values unchanged, because Python uses them:

- `add_above_button`
- `add_below_button`
- `remove_button`
- `add_material_above_button`
- `add_material_below_button`
- `unit_combo`
- `import_xpedition_button`
- `import_text_button`
- `export_xpedition_button`
- `export_text_button`
- `table`
- `detail_title_label`
- `editor_stack`
- `placeholder_page`
- `soldermask_page`
- `copper_page`
- `dielectric_page`
- `copper_type_combo`
- `copper_thickness_edit`
- `copper_roughness_label`
- `apply_copper_button`
- `apply_sym_copper_button`
- `trace_width_label`
- `trace_width_edit`
- `trace_spacing_label`
- `trace_spacing_edit`
- `calculate_button`
- `show_result_button`
- `calculate_result_label`
- `dielectric_type_combo`
- `dielectric_manufacturer_combo`
- `dielectric_family_combo`
- `material_filter_edit`
- `dielectric_material_combo`
- `apply_layer_button`
- `apply_sym_layer_button`
- `layer_frequency_combo`
- `global_frequency_combo`
- `apply_all_frequency_button`
- `readonly_thickness_label`
- `readonly_dk_label`
- `readonly_df_label`
- `readonly_freq_label`
- `symmetry_badge`
- `note_label`
- `preview_host`
- `metric_total_host`
- `metric_copper_host`
- `metric_units_host`
- `metric_rows_host`
- `main_splitter`

These are code-mounted placeholders:

- `preview_host`: live stackup custom widget is inserted here
- `metric_total_host`
- `metric_copper_host`
- `metric_units_host`
- `metric_rows_host`

After saving the `.ui` file, run `python main.py` again. The app loads the `.ui` file at runtime, so your updated layout appears on the next launch.
