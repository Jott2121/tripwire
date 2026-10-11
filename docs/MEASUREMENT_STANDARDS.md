# Measurement standards (M0)

Dated 2026-10-11. Machine-readable twin: `tripwire/standards.py` (tests in
`tests/test_standards.py` keep the two in sync: every column, source URL, and
open item named here must exist there, and vice versa).

This document answers one question for every field Tripwire serves: **what
unit, format, or convention does the public FAA data actually use, and how do
we know?** Nothing is guessed. Where no authoritative source settles an item it
is marked UNRESOLVED and listed in [Open items](#open-items) with the command
that will close it.

## Method and its limits

- The repo holds no real SDR rows (by design; see PR #6 `docs/DATA.md`), so the
  repo settled only the header (issue #4) and the two date examples.
- Everything else comes from FAA, CFR, and NASA sources. **Limit:** the agent
  sandbox's network policy denied `sdrs.faa.gov`, `www.faa.gov`, `ecfr.gov`,
  `govinfo.gov`, and `nasa.gov`, so primary documents were read through
  search-engine excerpts, not downloaded. Where an excerpt quoted the source
  verbatim the item is HIGH; where it paraphrased, MEDIUM. A reviewer with
  browser access can promote items by opening the cited URL.
- Confidence scale: HIGH (stated by the authoritative source), MEDIUM
  (consistent secondary sources or primary text seen only in excerpt), LOW
  (inference from examples), UNRESOLVED (no source settles it).

## Sources

| Id | Document | URL |
|----|----------|-----|
| FI | SDRS Field Instructions, FAA, dated 2021-10-01 (quick reference for FAA Form 8070-1) | https://sdrs.faa.gov/Documents/SDRS%20Field%20Instructions.pdf |
| SS | SDRS Instructions for Single Submission | https://sdrs.faa.gov/Documents/Instructions%20for%20Single%20Submission.pdf |
| BL | SDRS Batch File Layout | https://sdrs.faa.gov/Documents/Batch%20File%20Layout.pdf |
| DL | FAA "Download Service Difficulty Reports (SDR) by Year" | https://www.faa.gov/av-info/download_SDR |
| GA | SDRS general-aviation submission form (drop-down values visible in page text) | https://sdrs.faa.gov/SubmissionsGeneralAviation.aspx |
| JASC | FAA Joint Aircraft System/Component Code Table (PDF last updated 2008-10-27 per search metadata) | https://sdrs.faa.gov/documents/JASC_Code.pdf |
| 47.15 | 14 CFR 47.15, Registration number | https://www.ecfr.gov/current/title-14/section-47.15 |
| 1.1 | 14 CFR 1.1, Definitions ("time in service") | https://www.ecfr.gov/current/title-14/section-1.1 |
| 121.703 | 14 CFR 121.703, Service difficulty reports | https://www.ecfr.gov/current/title-14/section-121.703 |
| 8300.12 | FAA Order 8300.12 (corrosion level definitions) | https://www.faa.gov/documentLibrary/media/Order/8300.12.pdf |
| NASA | NASA-STD-8739.4A w/ Change 4, Workmanship Standard for Crimping, Interconnecting Cables, Harnesses, and Wiring | https://standards.nasa.gov/sites/default/files/standards/NASA/A/4/nasa-std-87394a_w_change_4_0.pdf |
| ADB | AviationDB SDR query page (third party; used only for a partial code list) | https://www.aviationdb.com/Aviation/SdrQuery.shtm |
| #4 | Tripwire issue #4 (header and date examples copied from a real SDR-2026.csv) | https://github.com/Jott2121/tripwire/issues/4 |
| PROF | Maintainer run of `scripts/profile_sdr.py` on `SDR-2026.csv`, 2026-10-11 (45,910 rows), relayed by the owner. Receipt to be committed as `reports/sdr_profile_2026.json` from a machine that can reach the FAA host; the agent sandbox cannot | (repo) |

Secondary sources consulted for the N-number and time-in-service items: the
FAA "Forming an N-Number" page
(https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/forming_nnumber/)
and AC 43-9C (maintenance records), both consistent with the CFR text.

## File-level conventions

| Item | Standard | Source | Confidence |
|------|----------|--------|------------|
| File | One CSV per year, `SDR-<year>.csv`, from `https://external.apic4e.faa.gov/sdrs/retrieve/SDR-<year>.csv` | DL, #4 | HIGH |
| Encoding | **UTF-8**: 0 decode errors under UTF-8, 151 under cp1252; `latin-1` (issue #4, PR #6) garbles 323 real rows | PROF | HIGH (`encoding` resolved) |
| Header | 76 named columns in the order listed in `tripwire/standards.py:SDR_COLUMNS`. The batch *upload* layout is pipe-delimited with "a total of 76 pipe characters", i.e. 77 fields, and numbers `StageOfOperationCode` as field 17 and `HowDiscoveredCode` as field 18, one earlier than the download header; so upload and download are different layouts. **Resolved:** the download has 76 columns in the expected order | BL (excerpt), #4, PROF | HIGH (`column_count` resolved) |
| Missing value | Empty field. Many fields are optional ("If applicable", "Not required to enter information"); no sentinel such as `N/A`, `0`, or `-` is documented | FI | MEDIUM |
| Rights | FAA-published; narrative is operator-submitted and never committed here | DL, `docs/DATA.md` (PR #6) | HIGH |
| Change risk | SDRS "What's New" announces required fields, stronger validation and batch-handling changes with release planned for summer 2026 | https://sdrs.faa.gov/WhatsNew.aspx | MEDIUM; see `sdrs_2026_upgrade` |

## Dates and time zones

| Field | Standard | Source | Confidence |
|-------|----------|--------|------------|
| `DifficultyDate` | `mm/dd/yyyy` ("Difficulty Date. (mm/dd/yyyy)" on the form); example `01/02/2026` in the CSV | GA, SS, #4 | HIGH |
| `SubmissionDate` | ISO-8601 with a UTC offset, e.g. `2026-01-02T14:09:41.140-05:00`. The offset in the one example is -05:00 (US Eastern standard time). **Resolved:** the offset is fixed at -05:00 in every month Jan-Oct 2026, so it does not follow DST | #4, PROF | HIGH (`submission_tz` resolved) |
| Ordering | Part 121 filers report each 24-hour period beginning at 0900 local time and have 96 hours to submit (weekend/holiday extensions apply). A difficulty date after the submission date is therefore impossible for any filer, and a lag of more than a few days is unusual for part 121 | 121.703(d) | HIGH (rule), MEDIUM (lag expectation) |
| Control-number date | `OperatorControlNumber` embeds the submission date as `YYYYMMDD` (see identifiers) | FI | HIGH |

No SDR field carries a time of day other than `SubmissionDate`. No field is
documented as UTC.

## Time in service and cycles

| Field | Standard | Source | Confidence |
|-------|----------|--------|------------|
| `AircraftTotalTime`, `EngineTotalTime`, `PropellerTotalTime`, `PartTotalTime`, `ComponentTotalTime` | **Hours** ("Total Time - hours"). "Whole hours" is stated only for component times; in the CSV every hour column is 100% whole numbers | FI, PROF | HIGH |
| Definition of the hour | 14 CFR 1.1 "time in service, with respect to maintenance time records, means the time from the moment an aircraft leaves the surface of the earth until it touches it at the next point of landing" (airborne time, not block time) | 1.1 (via AC 43-9C excerpt) | HIGH |
| `AircraftTotalCycles`, `EngineTotalCycles`, `PropellerTotalCycles`, `PartTotalCycles`, `ComponentTotalCycles` | Dimensionless count of cycles; "If applicable", so frequently empty | FI | HIGH |
| `PartTimeSince`, `ComponentTimeSince` | Hours since the most recent overhaul, repair, or inspection ("Time Since (hours)"); whole hours at component level | FI | HIGH |
| `PartSinceCode`, `ComponentSinceCode` | Which event the TimeSince counts from. The form offers Overhaul / Repair / Inspection / Reset; the stored code values are not visible in any source we could read | FI, GA | UNRESOLVED (`code_tables`) |
| Whole vs decimal in the CSV | **Resolved:** 100% whole numbers in every hour column of SDR-2026 | PROF | HIGH (`whole_hours` resolved) |

## Lengths and airframe coordinates

| Field | Standard | Source | Confidence |
|-------|----------|--------|------------|
| `CrackLength` | **Inches**: "In inches, indicate the length of the crack." (Single Submission instructions). Optional, and the 2021 field instructions say the field "will be removed in the future" | SS, FI | HIGH |
| `NumberOfCracks` | Count; optional, also slated for removal | FI | HIGH |
| `FuselageStationFrom`, `FuselageStationTo`, `WingStationFrom`, `WingStationTo`, `ButtLineFrom`, `ButtlineTo`, `WaterLineFrom`, `WaterLineTo` | Airframe station coordinates. On US-built aircraft these are inches from a manufacturer-defined datum (fuselage station: fore-aft; butt line: left-right of centreline; water line: vertical). The SDRS instructions give From/To locations but state no unit; the unit comes from general airframe practice, and the datum is per maintenance manual | FI (fields), general references | MEDIUM |
| `StringerFrom`, `StringerTo` | Stringer designation (text), with a side | FI | MEDIUM |
| `StringerFromSide`, `StringerToSide`, `WingStationFromSide`, `WingStationToSide`, `ButtLineFromSide`, `ButtlineToSide` | Left/right qualifiers; stored code values unknown | FI | UNRESOLVED (`code_tables`) |
| `CorrosionLevel` | Levels 1, 2, 3 as defined for corrosion prevention and control programs: Level 1 reworkable within manufacturer limits; Level 2 beyond those limits, requiring repair or replacement of a principal structural element; Level 3 a potential airworthiness concern needing prompt action. The CSV encoding (digit, word, or other) is unverified | 8300.12 (definitions), FI (field) | MEDIUM |

Conversion factors used by the build (exact by definition, in
`standards.CONVERSION_FACTORS`): 1 in = 25.4 mm; 1 h = 60 min;
1 lbf = 4.4482216152605 N.

## Identifiers

| Field | Standard | Source | Confidence |
|-------|----------|--------|------------|
| `OperatorControlNumber` | Up to 17 alphanumerics: the first four are the submitter's certificate-based designator, the next eight the submission date as `YYYYMMDD`, the rest a submitter-designed sequence ("any submitter-designed numbering system is acceptable"). Example `AAIA202110010001`. Note the FAA's own example is 16 characters against a stated 17 maximum. **Resolved:** 44,879 of 45,910 real rows match the shape; a mismatch is a warning, never a reject | FI, SS, PROF | HIGH (`control_number_shape` resolved) |
| `OperatorDesignator` | First four characters of the operator's certificate number. A repair station filing for an operator enters the operator's designator, not its own. Required unless Submitter Type is `Z` or SDR Type is `G` | FI | HIGH |
| `SubmitterDesignator` | FAA-assigned identifier of the filer; not editable | SS | HIGH |
| `RegistryNNumber` | US registration mark **without the leading N** ("Enter the aircraft registration number, excluding the 'N' for US registered aircraft"; "Do not enter 'N'"). Foreign aircraft: country code plus as much of the mark as fits. Mark shape per 14 CFR 47.15(b): after N, at most five symbols, being all digits, 1-4 digits + 1 letter, or 1-3 digits + 2 letters; letters I and O never used; the first zero must be preceded by a digit 1-9 (so no leading zero). N1-N99 are reserved for FAA use but are valid shapes. **Resolved:** 0% of real values start with N; 45,621 of 45,663 are valid without it | FI, SS, 47.15, PROF | HIGH (`registry_prefix` resolved) |
| `JASCCode` | Four digits. First two = system code (tracks the ATA-100 chapter), last two = component code. Deviations from ATA: turbine engines are chapter 72, reciprocating engines chapter 85. Titles for the six codes the synthetic generator uses: 2820 aircraft fuel distribution system; 3220 nose/tail landing gear; 3230 landing gear retract/extend system; 5320 fuselage miscellaneous structure; 5710 wing main frame structure; 7230 turbine engine compressor section | JASC | HIGH (structure), MEDIUM (titles, read via a lookup site mirroring the PDF) |
| `PartNumber`, `ComponentPartNumber` | "Enter the Manufacturer's part number, not an airline/internally generated number." No universal format; free alphanumeric text | FI | HIGH |
| `PartSerialNumber`, `ComponentSerialNumber`, `AircraftSerialNumber`, `EngineSerialNumber`, `PropellerSerialNumber` | Manufacturer's serial number, "if applicable" | FI | HIGH |
| `PartName` | "the applicable name given to the part by the manufacturer. Refer to drop-down table." | FI | HIGH |
| `AircraftMake`, `AircraftModel`, `EngineMake`, `EngineModel`, `PropellerMake`, `PropellerModel`, `PartMake`, `ComponentMake`, `ComponentModel`, `ComponentName`, `PartLocation`, `ComponentLocation`, `StructuralOther` | Free text | FI | HIGH / MEDIUM (locations) |
| `Discrepancy` | Operator-submitted narrative; never committed here | FI, DL | HIGH |

## Code lists

Lists marked *partial* were read from excerpts that cut off; the module stores
them with `complete=False`, and `CodeList.check()` returns `None` (unknown)
rather than `False` for a code not in a partial list, so a validator never
rejects a real code we simply have not seen. Nothing is marked complete until
the full SDRS look-up tables are read (`code_tables`).

| Field | Known values | Source | Confidence |
|-------|--------------|--------|------------|
| `SubmitterTypeCode` | A carrier part 121; B repair station part 145; C operator part 91; D mechanic; E carrier part 135; F manufacturer; G FAA; H other; I part 125; L part 133. Observed in SDR-2026: A B C D E G H I M P S W Z (`M` 7 times); meanings of M P S W Z not in any source read. *Partial* | GA, FI, PROF | MEDIUM |
| `NatureOfConditionA`, `NatureOfConditionB`, `NatureOfConditionC` | 1-3 codes, A required: A flame/fire; B smoke/fumes/odors/sparks; C F.O.D.; D inflight separation; E vibration/buffet; F flight control affected; G multiple failure; H electrical; list continues. *Partial* | GA, FI | MEDIUM |
| `PrecautionaryProcedureA`, `PrecautionaryProcedureB`, `PrecautionaryProcedureC`, `PrecautionaryProcedureD` | 1-4 codes (FI; SS says up to three): A unscheduled landing; B emergency descent; C aborted takeoff; D return to block; E engine shutdown; F activate fire extinguisher; J fuel dumped (from FI's worked example "E, J, A"). *Partial* | GA, FI | MEDIUM |
| `StageOfOperationCode` | Two letters (BL: "Alpha 2"), flight and ground stages: AA air ambulance; AB aerobatic; AG agriculture; AP approach; CL climb; CR cruise; DE descent; ET ETOPS; EX external load; FF fire fighting; HO hovering; list continues. *Partial* | BL, FI, ADB | HIGH (shape), MEDIUM (values) |
| `HowDiscoveredCode` | One alphanumeric (BL: "Alpha-numeric 1"); table not located | BL, FI | MEDIUM (shape), UNRESOLVED (values) |
| `SDRType` | One letter; observed values A and G (FI references `G`); meanings not located. *Partial* | FI, PROF | MEDIUM |
| `ReceivingRegionCode`, `ReceivingDistrictOffice` | FAA region (two letters in a third-party sample) and district office number; tables not located | FI | UNRESOLVED |
| `PartCondition` | From a drop-down table; values not located | FI | UNRESOLVED |
| `CorrosionLevel` | 1 / 2 / 3 (definitions above); stored encoding unverified | 8300.12 | MEDIUM |

## Spec-limit sources for M1

M1 needs public limit tables with units. The candidate named in the brief,
NASA-STD-8739.4A (crimp pull-test minimums by wire gauge, Table 12-1), is
tabulated in **newtons with pounds-force in parentheses** per every excerpt
seen, and the standard forbids pulling beyond 80 percent of the specified
minimum during a non-destructive pull test. Change 1 of the standard "removed
ambiguous, duplicate information from Table 12-1", so values must be taken from
the current change, not from third-party copies. The excerpts we could reach
were garbled (rows did not align with gauges), so **no value from that table is
recorded here**; see `nasa_8739_4a_table`. `standards.py` fixes the canonical
force unit as newton with the exact lbf factor so the table can be entered
later without a unit decision.

## PR #6 assumptions: verdicts

| PR #6 (branch `claude/issue-4`) assumption | Verdict | Evidence |
|---|---|---|
| `CrackLength` is in inches (`crack_length_in`; UNIT_SWAP factor 25.4) | **Confirmed** | FI: "In inches, indicate the length of the crack." Also note the field is slated for removal; do not make it the only length-bearing field in the task set |
| `AircraftTotalTime` is in hours (`aircraft_total_time_hours`; UNIT_SWAP factor 60) | **Confirmed** | FI: whole hours; 14 CFR 1.1 definition. The synthetic generator emits one decimal place (`f"{...:.1f}"`), which contradicts "whole hours"; profile a real file before deciding whether decimals ever occur |
| `AircraftTotalCycles` is a dimensionless count | **Confirmed** | FI |
| `DifficultyDate` is `MM/DD/YYYY` | **Confirmed** | GA, SS, #4 |
| `SubmissionDate` is ISO-8601 with offset, parsed tz-aware | **Consistent with the example**; zone semantics UNRESOLVED | #4 |
| `JASCCode` is exactly four digits | **Confirmed** | JASC |
| "77 columns" in prose, 76 names in the header | **Resolved**: the download has 76 columns in the listed order; 77 is the upload layout | PROF |
| Encoding `latin-1` | **Contradicted**: the file is UTF-8; latin-1 garbles 323 rows | PROF |
| Synthetic `SubmitterTypeCode` values `A`, `O`, `M` | **Contradicted for `O`** only: `M` occurs in real data (7 rows); `O` does not | GA, PROF |
| Synthetic `SDRType` value `SDR` | **Contradicted**: the field is a one-letter code | FI |
| Synthetic `StageOfOperationCode` values `PREFLIGHT`, `SCHEDULED`, `WALKAROUND`, `TEARDOWN` | **Contradicted**: the field is a two-letter code (e.g. `CR`, `AP`) | BL, FI |
| Synthetic `OperatorControlNumber` `OCN000001` | **Contradicted** by the documented shape (4-char designator + `YYYYMMDD` + sequence) | FI, SS |
| Synthetic rows leave `RegistryNNumber` empty | Not wrong, but when populated it must omit the `N` | FI, SS |
| `IMPOSSIBLE_DATE` corruption (difficulty after submission) | **Well-founded**: impossible under the reporting rule | 121.703(d) |

Recommended follow-up on PR #6 (not done here): replace the three contradicted
synthetic fields with codes from `standards.py`, generate control numbers in the
documented shape, read the file as UTF-8, and emit whole-hour total times.

## Open items

Each key exists in `standards.UNRESOLVED` (open) or `standards.RESOLVED` (closed)
with the same question. The profiling step is `scripts/profile_sdr.py` (PR #8).

### Resolved by the maintainer profile of SDR-2026 (PROF)

| Key | Question | Answer |
|-----|----------|--------|
| `column_count` | 76 or 77 columns? | 76, in the expected order; 77 is the upload layout |
| `encoding` | latin-1, UTF-8, or cp1252? | UTF-8 (0 errors); cp1252 151 errors; latin-1 garbles 323 rows |
| `submission_tz` | Fixed -05:00 or Eastern with DST? | Fixed -05:00 in every month Jan-Oct 2026 |
| `whole_hours` | Whole hours only, or decimals? | 100% whole numbers in every hour column |
| `registry_prefix` | Does the CSV omit the N? | Yes: 0% start with N; 45,621 of 45,663 valid without it |
| `control_number_shape` | Do real rows follow 4+8+sequence? | 44,879 of 45,910 do; a mismatch is a warning, not a reject |

The receipt behind these answers, `reports/sdr_profile_2026.json`, is not yet
committed: the agent sandbox cannot reach the FAA host. Until it is, the rows
above cite the relayed maintainer run. Observed stage-of-operation,
how-discovered and corrosion code values were not included in the relay and
are added when the report lands.

### Still open

| Key | Question | How to resolve |
|-----|----------|----------------|
| `code_tables` | Full tables for `SDRType`, region, district office, `HowDiscoveredCode`, `PartCondition`, since-codes, side codes, `CorrosionLevel`, and the rest of the three partial lists | Read the SDRS Look-up Tables (host blocked from the sandbox) or take distinct values from the committed profile |
| `nasa_8739_4a_table` | Exact Table 12-1 minimums per AWG in N and lbf | Open the NASA PDF directly; excerpts were garbled |
| `sdrs_2026_upgrade` | Does the summer-2026 SDRS release change the download layout? | Re-profile the first post-upgrade file and diff the header |
