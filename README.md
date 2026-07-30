# StackUp Editor

StackUp Editor is a Windows desktop application for designing, reviewing, and exchanging PCB stackups. It supports both conventional rigid boards and complex rigid-flex constructions with multiple connected rigid and flex parts.

The application combines material selection, structural validation, live visualization, impedance analysis, and CAD-oriented import/export tools in one interface.

## Main features

- Create rigid and rigid-flex PCB stackups.
- Add, remove, and edit copper and dielectric layers.
- Build multiple connected rigid and flex parts.
- View the construction in an interactive live stackup diagram.
- Select materials from manufacturer-based catalogs.
- Work with core, prepreg, no-flow prepreg, dummy core, etched core, flex core, coverlay, and solder mask materials.
- Apply symmetric changes when required.
- Validate common PCB construction rules.
- Calculate single-ended and differential impedance.
- Import and export StackUp Editor text files.
- Import and export Xpedition `.stk` stackups.

The live stackup is a structural visualization. Layer sizes are arranged for readability and are not drawn directly to physical thickness scale.

## Who is it for?

StackUp Editor is intended for PCB designers, hardware engineers, signal-integrity engineers, rigid-flex designers, CAM engineers, and students working with PCB layer constructions.

## Getting started

### Requirements

- Windows
- Python 3.10 or newer
- PySide6
- Node.js for impedance calculations

### Installation

```powershell
git clone https://github.com/ozmazm/StackupBuilder.git
cd StackupBuilder

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
python main.py
```

For catalog-building and packaging tools:

```powershell
python -m pip install -r requirements-dev.txt
```

## Basic workflow

1. Start the application and choose **Rigid Stackup** or **Rigid-Flex Stackup**.
2. Build the required copper and dielectric structure.
3. Select materials and constructions from the stackup table.
4. Review the live stackup and structural warnings.
5. Configure impedance profiles when required.
6. Save the project or export it as text, Excel, or Xpedition `.stk`.

## Material data

The repository includes material catalogs for rigid laminates, prepregs, flex cores, coverlays, and no-flow materials. Material properties may vary by frequency and manufacturer revision.

Always confirm final material availability, Dk, Df, thickness, resin content, copper profile, and manufacturing constraints with the selected PCB fabricator.

## Engineering notice

StackUp Editor is an engineering aid. Its validation rules, visualizations, and impedance results do not replace final review by a PCB manufacturer or signal-integrity specialist.

## License

No license has been selected for this repository yet.
