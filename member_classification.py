"""Approved membership classifications and conservative CW/CE PDF recognition."""
from __future__ import annotations

import math
import re

CLASSIFICATIONS = (
    'Apprentice - Inside', 'Apprentice - Outside', 'Apprentice - Tech', 'BA member',
    'Cable Splicer - Inside', 'Cable Splicer - Outside', 'CE/CW', 'Construction Lineman',
    'Construction Substation Technician', 'Dropped', 'Equipment Operator', 'Fire Alarm',
    'Fire Alarm|Journeyman', 'Foreman', 'General Foreman', 'Groundman',
    'ISMA Cert(International Municipal Signal Assoc)', 'Journeyman', 'Journeyman Electrician',
    'Journeyman Lineman', 'Journeyman Substation Technician', 'Lineman Class A',
    'Lineman Class B', 'Lineman Class C', 'Low Voltage Cable Puller', 'Low Voltage Tech',
    'Maintenance Technician', 'Master Electrician', 'None', 'Operator', 'Owner',
    'Owner/Master Electrician', 'Project Manager (Estimator)', 'Relay Tester', 'Residential',
    'Retired', 'Shop Management', 'Shop Personnel', 'Substation Technician Apprentice',
    'Supervision', 'Tele Data Apprentice', 'Tele Data Groundman', 'Tele Data Journeyman',
    'Tele Data Lineman', 'Tele Data Operator', 'Tele Data Tech', 'Trainee/Helper',
)


def normalize_classification(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Choose a classification from the approved list.')
    key = ' '.join(value.split()).casefold()
    # These levels are explicitly mapped to CE/CW, not inferred from hours.
    if re.fullmatch(r'(?:cw|ce)(?:[ -]+(?:[ivx]+|[1-9][0-9]*))?', key):
        return 'CE/CW'
    for name in CLASSIFICATIONS:
        if key == name.casefold():
            return name
    raise ValueError('Classification is missing or unrecognized. Choose an approved classification after reviewing the PDF.')


CHECKBOX_ROWS = (677.90, 660.19, 642.49, 625.15, 608.20, 591.24, 574.28, 557.32)


def cwce_classification(page, fragments) -> str | None:
    """Read only the verified CW/CE sheet's vector radio marks.

    The source export paints identical blank box images at eight positions,
    then a filled 12-curve ring/dot over the selected box. Requiring both the
    template anchors and that exact mark prevents printed options or hours
    being mistaken for a selected classification. Other formats need review.
    """
    if page.rotation % 360 or float(page.get('/UserUnit', 1)) != 1:
        return None
    for box in (page.mediabox, page.cropbox):
        if any(not math.isclose(float(actual), expected, abs_tol=.1)
               for actual, expected in zip(box, (0, 0, 612, 792))):
            return None
    def text_at(left, bottom, right, top):
        return ' '.join(f.text.strip() for f in fragments
                        if left <= f.x < right and bottom <= f.y < top and f.horizontal).strip()
    if (text_at(100, 624, 140, 627) != 'CW IV'
            or text_at(100, 573, 140, 576) != 'CE III'
            or text_at(100, 556, 160, 560) != 'JOURNEYMAN'
            or 'classification.' not in text_at(420, 708, 510, 713)):
        return None
    boxes, marks = set(), set()
    points = []
    curves = 0
    invalid = False
    def visitor(op, args, cm, tm):
        nonlocal points, curves, invalid
        a, b, c, d, e, f = map(float, cm)
        if op == b'Do' and abs(e - 81) < .3 and abs(a - 9) < .1 and abs(d - 8.25) < .1 and abs(b) < .01 and abs(c) < .01:
            for index, y in enumerate(CHECKBOX_ROWS):
                if abs(f - y) < .3:
                    boxes.add(index)
        elif op in (b'm', b'l', b'c'):
            points.extend((float(args[i])*a + float(args[i+1])*c + e,
                           float(args[i])*b + float(args[i+1])*d + f) for i in range(0, len(args), 2))
            curves += op == b'c'
        elif op == b're':
            # Rectangle paths belong to the printed table/clip, not a mark.
            points = []
            curves = 0
        elif op in (b'n', b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*'):
            if points and all(77 < x < 96 and 550 < y < 692 for x, y in points):
                bounds = min(x for x,y in points), min(y for x,y in points), max(x for x,y in points), max(y for x,y in points)
                cx, cy = (bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2
                row = next((i for i,y in enumerate(CHECKBOX_ROWS) if abs(cy - (y+4.125)) < 2), None)
                if (op in (b'f', b'F') and curves == 12 and abs(cx-85.5) < 2
                        and abs(bounds[2]-bounds[0]-9) < .3 and abs(bounds[3]-bounds[1]-9) < .3 and row is not None):
                    marks.add(row)
                elif op != b'n':
                    invalid = True
            points, curves = [], 0
    page.extract_text(visitor_operand_before=visitor)
    if invalid or boxes != set(range(8)) or len(marks) != 1:
        return None
    return 'Journeyman' if marks == {7} else 'CE/CW'


def extract_classification(reader, read_fragments) -> str | None:
    found = []
    for page in reader.pages:
        fragments = read_fragments(page)
        if any('classification.' in f.text for f in fragments):
            found.append(cwce_classification(page, fragments))
    return found[0] if len(found) == 1 else None
