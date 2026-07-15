# StackUp Editor

StackUp Editor is a desktop application for creating, editing, visualizing, validating, and exchanging PCB stackups. It supports conventional rigid boards as well as multi-zone rigid-flex constructions with linked rigid and flex regions.

The application combines a structured layer table, manufacturer-based material catalogs, symmetric editing tools, a live cross-section, rigid-flex synchronization, and an integrated impedance workflow in one engineering-focused interface.

## What is it for?

StackUp Editor helps turn an initial PCB layer concept into a structured stackup definition that can be reviewed, adjusted, analyzed, and shared.

Typical uses include:

- Defining copper, core, prepreg, solder-mask, flex-core, coverlay, and adhesive layers.
- Comparing dielectric constructions and frequency-dependent material properties.
- Maintaining symmetric rigid stackups while adding or removing layers.
- Building rigid-flex constructions with several rigid and flex zones.
- Keeping flex-core copper weights synchronized with the linked rigid stackup.
- Checking structural rules before a stackup is sent for fabrication review.
- Preparing single-ended and differential impedance profiles.
- Importing existing stackups and exporting them for documentation or CAD workflows.

The live preview is intentionally schematic. Rectangle sizes use stable structural proportions so layer relationships remain readable; they are not drawn to scale from the physical thickness values in the table.

## Who is it for?

StackUp Editor is intended for:

- PCB layout engineers
- Hardware and signal-integrity engineers
- Rigid-flex designers
- PCB fabrication and CAM engineers
- Engineering teams preparing stackup proposals
- Students and researchers learning PCB construction

It is especially useful when the design contains multiple flex sandwiches or when material, copper, symmetry, and impedance decisions must remain coordinated across several zones.

## Core capabilities

### Rigid stackup editing

- Add copper layers above or below the selected location.
- Add dielectric materials above or below a selected dielectric.
- Apply copper and dielectric changes individually or symmetrically.
- Remove symmetric copper or dielectric pairs when the resulting construction remains valid.
- Edit solder-mask properties directly in the stackup table.
- View total board thickness, copper count, row count, and display units.
- Detect symmetry and material-structure warnings.

### Rigid-flex editing

- Start with a rigid zone followed by a flex zone.
- Add or remove alternating rigid and flex zones.
- Insert and remove flex sandwiches.
- Display all linked zones in a combined live stackup.
- Select layers from either the table or the combined preview.
- Synchronize flex-core copper type and thickness with corresponding rigid copper rows.
- Keep coverlay and adhesive layers attached to the correct flex sandwich.
- Preserve structurally correct relative spans after sandwiches are inserted or removed.

### Structural safeguards

The editor enforces several construction rules during rigid-flex editing:

- Rigid Core and Flex Core materials cannot directly follow another core in dielectric order.
- At least one Rigid PP layer must remain between neighboring core materials.
- Additional bridge prepreg can be inserted and removed, but the last required separator is protected.
- Flex-core rows and their linked copper layers are edited from the flex zone.
- Symmetric operations update both sides of the rigid stackup together.
- Copper removal is limited when it would invalidate the linked flex construction.

These checks are design aids, not a replacement for fabricator-specific design-rule review.

## Material catalogs

The repository includes JSON catalogs generated from local manufacturer datasheets.

| Catalog | Current contents | Important fields |
| --- | ---: | --- |
| Rigid core and prepreg | 732 entries: 460 core and 272 prepreg | Manufacturer, family, construction, resin content, thickness, Dk, Df, and frequency |
| Flex core | 26 Panasonic R-F777 constructions | ED/RA copper type, upper and lower copper thickness, polyimide thickness, Dk, Df, and frequency |
| Coverlay | 2 Arisawa C33 components | Polyimide and adhesive thickness, Dk, Df, frequency, and available datasheet properties |

The rigid catalog currently contains material data from Isola, Panasonic, Nelco, TUC, and Shengyi families.

### Frequency-dependent properties

Material entries may contain Dk and Df values at several frequencies. The selected layer frequency controls the values used by the stackup and impedance calculations. A global frequency action is also available for applying a frequency selection across rigid dielectric layers.

### Flex-core copper synchronization

Flex-core product names encode the upper copper, dielectric, and lower copper construction. For example:

- `35-25-35` uses 35 µm copper on both sides, displayed as 1 oz.
- `18-25-18` uses 18 µm copper on both sides, displayed as 0.5 oz.

Selecting a flex-core material updates the linked rigid copper rows automatically.

### Catalog files

- `data/material_catalog.json` — rigid core and prepreg catalog
- `stackup_editor/flex_core_material_catalog.json` — bundled flex-core catalog
- `stackup_editor/coverlay_material_catalog.json` — bundled coverlay catalog
- `Materials/` — source manufacturer PDFs used by the catalog builders

Always verify catalog values against the latest manufacturer datasheet and the fabrication frequency/model required by your board supplier.

## Impedance workflow

The Calculate Impedance workspace supports:

- Single-ended and differential profiles
- Multiple target-impedance profiles
- Per-layer reference-plane selection
- Trace-width and differential-gap entry
- Automatic single-ended width solving toward a target impedance
- Copper-thickness and roughness inputs
- Dielectric Dk, Df, and frequency data from the active stackup
- Detailed field-solver reports and parameter sweeps
- Excel impedance-table export using `TransmissionLineTemp.xlsx`

The field solver is launched through Node.js and the JavaScript solver sources under `js_2d_fields-master/`.

## Import and export

The File menu is available from the main StackUp Editor window.

Supported workflows include:

- Import a StackUp Editor text export.
- Export a human-readable stackup text file.
- Import an Xpedition `.stk` stackup.
- Export the current stackup as an Xpedition `.stk` file.
- Export impedance profiles to an `.xlsx` workbook.

The text format can retain StackUp Editor impedance-workspace data in addition to the visible stackup definition.

## How to use

### 1. Choose a mode

At startup, select:

- **Rigid Stackup** for a conventional rigid PCB.
- **Rigid Flex Stackup** for linked rigid and flex zones.

### 2. Build the layer structure

Select a row in the stackup table and use the structural controls:

- **Add Layer Above**
- **Add Layer Below**
- **Add Material Above**
- **Add Material Below**
- **Remove Symmetric Pair**

Rigid structural operations are symmetric by design.

### 3. Assign materials

Select a dielectric row, then filter by:

1. Dielectric type
2. Manufacturer
3. Family
4. Material entry
5. Frequency

Apply the result to the selected layer or its symmetric pair. Copper rows provide copper-type, thickness, and surface-roughness controls.

### 4. Work with rigid-flex zones

In rigid-flex mode:

1. Edit flex-core and coverlay materials from the Flex tab.
2. Insert or remove flex sandwiches as required.
3. Add rigid prepreg in valid bridge locations between flex cores.
4. Use **Add Zone** to extend the rigid/flex sequence.
5. Review the combined live stackup after every structural change.

### 5. Calculate impedance

Open **Calculate Impedance**, choose the single-ended or differential section, create a target profile, assign reference planes, and enter the trace geometry. Run the solver for individual rows or export the completed impedance table.

### 6. Save or exchange the result

Use the File menu to export a text or Xpedition stackup. Importing the exported text later restores the supported stackup and impedance-workspace data.

## Installation

The application is currently developed and packaged for Windows.

### Requirements

- Python 3.10 or newer
- PySide6, including Qt WebEngine support
- Node.js for the field solver
- `pypdf` for rebuilding the rigid material catalog
- `pdfplumber` for additional PDF table parsing support
- PyInstaller only when building a portable release

### Development setup

From PowerShell:

```powershell
git clone <your-repository-url>
cd "StackUpEditor"

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python main.py
```

If `node.exe` is not available on `PATH`, set `STACKUP_EDITOR_NODE` to its full path before running impedance calculations or building a release.

```powershell
$env:STACKUP_EDITOR_NODE = "C:\Program Files\nodejs\node.exe"
python main.py
```

## Building a portable Windows release

The release script bundles the Python application, required project data, and a Node.js runtime for the field solver.

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_release.ps1
```

The portable application and ZIP archive are written to `release_dist/`.

Optional environment overrides:

```powershell
$env:STACKUP_EDITOR_PYTHON = "C:\Path\To\python.exe"
$env:STACKUP_EDITOR_NODE = "C:\Path\To\node.exe"
```

## Rebuilding material catalogs

To rebuild the rigid core/prepreg catalog, first place the supported manufacturer PDFs under a local `Materials/` directory, then run:

```powershell
python .\tools\build_material_catalog.py
```

Rebuild the rigid-flex catalogs:

```powershell
python .\stackup_editor\build_flex_material_catalog.py
```

Review the generated JSON diff carefully before committing catalog changes. Datasheet table formats can change, and parsed engineering values should always be audited.

## Project structure

```text
StackUpEditor/
├── main.py                         Application entry point
├── main.spec                       PyInstaller configuration
├── data/                           Generated rigid-material catalog
├── Materials/                      Optional local datasheets; excluded from Git
├── runtime/                        Runtime files used by packaged builds
├── requirements.txt                Runtime Python dependency
├── requirements-dev.txt            Catalog and packaging dependencies
├── stackup_editor/
│   ├── models.py                   Stackup data model and structural rules
│   ├── qt_app.py                   Main rigid editor window
│   ├── rigid_flex_app.py           Multi-zone rigid-flex editor and preview
│   ├── catalog.py                  Rigid material catalog loader
│   ├── flex_catalog.py             Flex-core and coverlay catalog loaders
│   ├── exporter.py                 Text and Xpedition import/export
│   ├── impedance_dialog.py         Impedance workspace UI
│   ├── field_solver_bridge.py      Python-to-Node field-solver bridge
│   ├── impedance_table_export.py   Excel impedance-table export
│   ├── units.py                    Unit conversion and formatting
│   └── ui/                         Qt Designer interface files
├── tools/
│   ├── build_material_catalog.py   Rigid catalog generator
│   ├── build_release.ps1           Portable Windows release builder
│   └── field_solver_runner.mjs     JavaScript field-solver runner
└── js_2d_fields-master/            JavaScript/WASM field-solver sources
```

## Units and copper types

Supported display units:

- µm
- mm
- mil
- inch
- oz for copper weight

Supported rigid copper labels include RTF, VLP, HVLP, and STD. Flex-core constructions support ED and RA copper types.

## Current limitations

- The live stackup is a structural diagram, not a thickness-scaled mechanical drawing.
- Rigid-flex rules are general safeguards and may not match every fabricator's process.
- Imported Xpedition files must use the subset of stackup data supported by the current parser.
- Material properties depend on the bundled catalog revision and selected frequency.
- Field-solver results depend on the selected reference planes, geometry, material data, and solver model.
- The release workflow is Windows-focused.

## Contributing

Contributions are welcome for new material parsers, additional CAD exchange formats, validation rules, solver improvements, automated tests, and UI refinements.

When contributing:

1. Keep structural editing symmetric unless a workflow explicitly requires otherwise.
2. Preserve rigid-flex links when changing layer-index logic.
3. Add validation for new material or construction rules.
4. Verify live-preview behavior after inserting and removing middle flex sandwiches.
5. Audit generated material-catalog changes against their source datasheets.
6. Test both Rigid Stackup and Rigid Flex Stackup modes.

## Engineering notice

StackUp Editor is an engineering aid. Final stackups, impedance targets, material availability, copper tolerances, press thicknesses, and rigid-flex constructions must be reviewed with the selected PCB fabricator before release to manufacturing.

## License

No license has been selected for this repository yet. Add a `LICENSE` file before distributing or accepting external contributions.
