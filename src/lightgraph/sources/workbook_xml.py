"""Bounded checks shared by workbook readers before parsing XML parts."""


def forbidden_xml_declaration(data: bytes) -> bool:
    markup = data.replace(b"\x00", b"").upper()
    return b"<!DOCTYPE" in markup or b"<!ENTITY" in markup


def workbook_has_declarations(archive) -> bool:
    for part in archive.infolist():
        if not part.filename.lower().endswith((".xml", ".rels")):
            continue
        with archive.open(part) as stream:
            tail = b""
            while block := stream.read(65536):
                data = tail + block
                if forbidden_xml_declaration(data):
                    return True
                tail = data[-64:]
    return False
