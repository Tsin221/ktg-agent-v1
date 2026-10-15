"""Bind an already executed acceptance operation to a disposable Agent SQLite DB.

This is for browser result-recovery testing only; it does not submit a write.
"""

import sqlite3
import sys

from ktg_agent.ktg.inventory import RECEIPT, InventoryStore


db_path, operation_id, document_id, document_code = sys.argv[1:5]
store = InventoryStore(db_path)
generated = store.create("id:1", RECEIPT, {
    "documentId": int(document_id), "documentCode": document_code,
    "documentStatus": "APPROVED", "documentVersion": 4,
    "additions": [{"sourceDocLineId": 1, "quantity": 2}],
})
with sqlite3.connect(db_path) as db:
    db.execute("UPDATE inventory_previews SET operation_id=?, state='submitted' WHERE operation_id=?",
               (operation_id, generated))
