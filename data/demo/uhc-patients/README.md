# UnitedHealthcare synthetic demo patients

Self-contained synthetic charts for ClearPath demos. Not real PHI.

Folder: `data/demo/uhc-patients/`

**Orders use live Dual Complete OH-S3 catalog labels** (not fictional CPT names).

## Quick try list

### PA required (catalog)
| Patient | Order (live label) |
|---|---|
| Robert Nguyen | Outpatient diagnostic tests |
| Priya Sharma | Outpatient diagnostic tests - X-rays |
| Marcus Bennett | Chiropractic services |
| Diego Ramirez | Outpatient diagnostic tests |
| Nina Castillo | Outpatient rehabilitation services |
| Kevin Owens | Outpatient diagnostic tests |
| Grace Kim | Outpatient diagnostic tests |

### Conditional
| Patient | Order |
|---|---|
| Linda Okonkwo | Ambulance services |

### No PA / preventive
| Patient | Order |
|---|---|
| Helen Park | Annual wellness visit |
| Anthony Brooks | Outpatient diagnostic tests - X-rays |

Selecting a patient on the Order Desk auto-fills insurer, plan, and the suggested order.
Upload PDFs in `demo-patient-reports/` for the Run coverage → questionnaire path.
