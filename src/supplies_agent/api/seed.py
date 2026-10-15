"""Seed data. Supplier names are fictional. QuickChem is deliberately not approved."""

SUPPLIERS = [
    # (id, name, approved)
    ("SUP-NW", "Northwind Scientific", True),
    ("SUP-BL", "BioLine Supplies", True),
    ("SUP-LC", "LabCore Direct", True),
    ("SUP-QC", "QuickChem Discount", False),
]

CATALOG_ITEMS = [
    # (sku, name, category, supplier_id, pack_size, unit_price_cents, lead_time_days)
    ("NW-TRIS-1KG", "Tris base, molecular biology grade", "reagent", "SUP-NW", "1 kg", 8900, 3),
    ("NW-ETOH-2L5", "Ethanol absolute, >=99.8%", "reagent", "SUP-NW", "2.5 L", 6400, 2),
    ("NW-NACL-1KG", "Sodium chloride, ACS reagent", "reagent", "SUP-NW", "1 kg", 3200, 2),
    ("BL-PBS-500", "PBS 1X, pH 7.4, sterile", "reagent", "SUP-BL", "500 mL", 1850, 2),
    ("BL-AGAR-100", "Agarose, low EEO", "reagent", "SUP-BL", "100 g", 11500, 4),
    ("BL-DMEM-500", "DMEM, high glucose", "cell_culture", "SUP-BL", "500 mL", 2600, 3),
    (
        "BL-FBS-500",
        "Fetal bovine serum, heat inactivated",
        "cell_culture",
        "SUP-BL",
        "500 mL",
        49000,
        7,
    ),
    ("BL-TRYP-100", "Trypsin-EDTA 0.25%", "cell_culture", "SUP-BL", "100 mL", 2100, 3),
    (
        "LC-TIP-200F",
        "Filter pipette tips, 200 uL",
        "consumable",
        "SUP-LC",
        "10 x 96 racks",
        7800,
        2,
    ),
    (
        "LC-TIP-1000F",
        "Filter pipette tips, 1000 uL",
        "consumable",
        "SUP-LC",
        "10 x 96 racks",
        8400,
        2,
    ),
    ("LC-TUBE-50", "Conical centrifuge tubes, 50 mL", "consumable", "SUP-LC", "25 tubes", 1500, 1),
    ("LC-GLOVE-M", "Nitrile gloves, size M", "consumable", "SUP-LC", "100 gloves", 1200, 1),
    ("QC-ETOH-2L5", "Ethanol absolute, technical grade", "reagent", "SUP-QC", "2.5 L", 2900, 5),
]

STOCK = [
    # (sku, location, quantity)
    ("NW-TRIS-1KG", "Chemical store B", 3),
    ("NW-ETOH-2L5", "Flammables cabinet", 1),
    ("NW-NACL-1KG", "Chemical store B", 4),
    ("BL-PBS-500", "Cold room 2", 6),
    ("BL-AGAR-100", "Chemical store B", 2),
    ("BL-DMEM-500", "Cold room 2", 2),
    ("BL-FBS-500", "Freezer -20C", 0),
    ("LC-TIP-200F", "Lab 3.12 shelf", 1),
    ("LC-TIP-1000F", "Lab 3.12 shelf", 5),
    ("LC-TUBE-50", "Lab 3.12 shelf", 8),
    ("LC-GLOVE-M", "Lab 3.12 shelf", 0),
]
