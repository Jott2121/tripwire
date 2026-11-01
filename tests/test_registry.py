"""Registry loader: synthetic files only (fictional marks, makers and serials). No network."""

from __future__ import annotations

from dataclasses import fields
from datetime import date

import pytest

from tripwire import registry as r

# The text file's short header with the personal columns present but blank: the loader must not
# read them. Trailing comma as in the real files' layout (an empty last column).
MASTER_HEADER = (
    "N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,"
    "STREET2,CITY,STATE,ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,"
    "CERTIFICATION,TYPE AIRCRAFT,TYPE ENGINE,STATUS CODE,MODE S CODE,FRACT OWNER,"
    "AIR WORTH DATE,OTHER NAMES(1),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,"
    "MODE S CODE HEX,"
)


def master_row(n, serial, code, year, cert="20200101", status="V", name="REDACTED"):
    cols = [n, serial, code, "41514", year, "1", name, "1 TEST WAY", "", "TESTVILLE", "XX",
            "00000", "1", "001", "US", "20240101", cert, "1N", "4", "5", status, "50000000",
            "", "20200101", "", "20300101", "00000001", "", "", "A00000", ""]  # fmt: skip
    return ",".join(f"{c:<8}" if i in (0, 1) else c for i, c in enumerate(cols))


ACFTREF = (
    "CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,AC-WEIGHT,SPEED,"
    "TC-DATA-SHEET,TC-DATA-HOLDER,\n"
    "0010101,ACME AERO WORKS          ,AW-100    ,4,1,1,0,01,004,CLASS 1,0120,A0TEST,ACME,\n"
    "0020202,ZENITH AIRCRAFT CORP     ,ZX-9      ,5,5,1,0,02,012,CLASS 2,0200,A1TEST,ZEN,\n"
)


@pytest.fixture
def files(tmp_path):
    master = tmp_path / "MASTER.txt"
    rows = [
        master_row("101AB", "ASN1001", "0010101", "2005"),
        master_row("202", "ASN2002", "0020202", "2019", cert="20260601"),
        master_row("303", "ASN3003", "0010101", ""),
        master_row("303", "ASN3004", "0010101", "2001"),  # listed twice
    ]
    master.write_text(MASTER_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    ref = tmp_path / "ACFTREF.txt"
    ref.write_text(ACFTREF, encoding="utf-8")
    return master, ref


def test_loads_identity_fields_and_never_the_registrant(files):
    master, ref = files
    m = r.load_master(master)
    assert len(m.records) == 4 and not m.rejects
    first = m.records[0]
    assert first == r.RegistryRecord("101AB", "ASN1001", "0010101", 2005, "4", "V",
                                     date(2020, 1, 1))  # fmt: skip
    names = {f.name for f in fields(r.RegistryRecord)}
    assert not names & {"name", "street", "city", "state", "zip", "registrant"}
    assert all("REDACTED" not in str(rec) and "TEST WAY" not in str(rec) for rec in m.records)
    refs = r.load_reference(ref)
    assert [x.manufacturer for x in refs.records] == ["ACME AERO WORKS", "ZENITH AIRCRAFT CORP"]
    assert refs.records[1].engines == 2 and refs.records[1].seats == 12


def test_document_spellings_are_accepted(tmp_path):
    p = tmp_path / "m.txt"
    head = ("N-Number,Serial Number,Aircraft Mfr Model Code,Year Mfr,Type Aircraft,Status Code,"
            "Certificate Issue Date,Registrant's Name")  # fmt: skip
    p.write_text(head + "\nN77,S1,0010101,1999,4,V,,SOMEONE\n", encoding="utf-8-sig")
    rec = r.load_master(p).records[0]
    assert rec.n_number == "77" and rec.cert_issue_date is None


def test_fail_closed_on_bad_files(tmp_path):
    with pytest.raises(r.RegistryFormatError, match="not found"):
        r.load_master(tmp_path / "none.txt")
    empty = tmp_path / "e.txt"
    empty.write_text("")
    with pytest.raises(r.RegistryFormatError, match="empty"):
        r.load_master(empty)
    bad = tmp_path / "b.txt"
    bad.write_bytes(MASTER_HEADER.encode() + b"\n\xff\xfe\n")
    with pytest.raises(r.RegistryFormatError, match="byte offset"):
        r.load_master(bad)
    short = tmp_path / "s.txt"
    short.write_text("N-NUMBER,SERIAL NUMBER\n1,2\n")
    with pytest.raises(r.RegistryFormatError, match="mfr_mdl_code"):
        r.load_master(short)


def test_malformed_rows_are_rejected_with_their_line(tmp_path):
    p = tmp_path / "m.txt"
    rows = [
        master_row("ABC", "S", "0010101", "2000"),  # N-number must start with a digit
        master_row("12", "S", "001", "2000"),  # code not 7 characters
        master_row("13", "S", "0010101", "20x0"),
        master_row("14", "S", "0010101", "2000", cert="2020-01-01"),
        "15,too,few",
    ]
    p.write_text(MASTER_HEADER + "\n" + "\n".join(rows) + "\n\n")
    res = r.load_master(p)
    assert res.records == [] and [x.line for x in res.rejects] == [2, 3, 4, 5, 6]
    ref = tmp_path / "r.txt"
    ref.write_text(ACFTREF.splitlines()[0] + "\n12,X,Y,4,1,1,0,1,1,C,1,T,H,\n")
    assert r.load_reference(ref).rejects[0].reason.startswith("code is not 7")


def test_registry_join_drops_ambiguous_marks(files):
    master, ref = files
    reg = r.Registry.build(r.load_master(master).records, r.load_reference(ref).records)
    assert reg.duplicate_n_numbers == {"303"} and reg.lookup("N303") is None
    rec = reg.lookup("n101ab")
    assert rec is not None and reg.reference(rec).model == "AW-100"
    assert r.norm_n_number("N") == "N" and r.norm_n_number(" n12 ") == "12"


def test_contract_check_uses_no_truth_store(files):
    master, ref = files
    reg = r.Registry.build(r.load_master(master).records, r.load_reference(ref).records)
    rec = reg.lookup("101AB")
    assert r.contract_failures(rec, reg.reference(rec)) == []
    odd = r.RegistryRecord("X", "S", "9999999", 1700, "4", "V", None)
    assert r.contract_failures(odd, None) == [
        "malformed:n_number", "implausible:year_mfr", "unresolved:mfr_mdl_code",
    ]  # fmt: skip
    wrong_ref = reg.by_code["0020202"]
    assert r.contract_failures(rec, wrong_ref) == ["mismatch:mfr_mdl_code"]
    missing = r.RegistryRecord("12", None, "0010101", None, "4", "V", None)  # type: ignore[arg-type]
    assert r.contract_failures(missing, reg.by_code["0010101"]) == ["missing:serial_number"]
