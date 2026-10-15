"""Read-only inventory acceptance audit for the isolated S3 MySQL database.

Requires mysql-connector-python and MYSQL_ROOT_PASSWORD in the local environment.
Prints aggregate evidence only; credentials and request bodies are never emitted.
"""

import argparse
import json
import os
import sys

import mysql.connector


def scalar(cursor, sql):
    cursor.execute(sql)
    return cursor.fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="ktg_s208_acceptance")
    args = parser.parse_args()
    if args.database != "ktg_s208_acceptance":
        parser.error("this audit is restricted to ktg_s208_acceptance")

    connection = mysql.connector.connect(
        host="127.0.0.1", port=3306, user="root",
        password=os.environ["MYSQL_ROOT_PASSWORD"], database=args.database,
    )
    try:
        connection.start_transaction(consistent_snapshot=True, readonly=True)
        cursor = connection.cursor()
        evidence = {
            "database": scalar(cursor, "SELECT DATABASE()"),
            "mysql_version": scalar(cursor, "SELECT VERSION()"),
            "receipt_success": scalar(cursor, "SELECT COUNT(*) FROM wm_inventory_operation WHERE action_type='ITEM_RECEIPT_EXECUTE' AND operation_status='succeeded'"),
            "issue_success": scalar(cursor, "SELECT COUNT(*) FROM wm_inventory_operation WHERE action_type='PRODUCTION_ISSUE_EXECUTE' AND operation_status='succeeded'"),
            "receipt_failed_or_rejected": scalar(cursor, "SELECT COUNT(*) FROM wm_inventory_operation WHERE action_type='ITEM_RECEIPT_EXECUTE' AND operation_status IN ('failed','rejected')"),
            "issue_failed_or_rejected": scalar(cursor, "SELECT COUNT(*) FROM wm_inventory_operation WHERE action_type='PRODUCTION_ISSUE_EXECUTE' AND operation_status IN ('failed','rejected')"),
            "transactions": scalar(cursor, "SELECT COUNT(*) FROM wm_transaction"),
            "transaction_links": scalar(cursor, "SELECT COUNT(*) FROM wm_inventory_operation_transaction"),
        }
        checks = {
            "negative_stock": "SELECT COUNT(*) FROM wm_material_stock WHERE quantity_onhand < 0",
            "duplicate_stock_dimension": "SELECT COUNT(*) FROM (SELECT stock_dimension_key FROM wm_material_stock GROUP BY stock_dimension_key HAVING COUNT(*) > 1) d",
            "duplicate_source_line_action": "SELECT COUNT(*) FROM (SELECT source_doc_type,source_doc_id,source_doc_line_id,transaction_type FROM wm_transaction WHERE source_doc_type IN ('IR','ISSUE') GROUP BY 1,2,3,4 HAVING COUNT(*) > 1) d",
            "unlinked_inventory_transaction": "SELECT COUNT(*) FROM wm_transaction t LEFT JOIN wm_inventory_operation_transaction l ON l.transaction_id=t.transaction_id WHERE t.source_doc_type IN ('IR','ISSUE') AND l.transaction_id IS NULL",
            "orphan_transaction_link": "SELECT COUNT(*) FROM wm_inventory_operation_transaction l LEFT JOIN wm_transaction t ON t.transaction_id=l.transaction_id WHERE t.transaction_id IS NULL",
            "non_success_with_link": "SELECT COUNT(*) FROM wm_inventory_operation o JOIN wm_inventory_operation_transaction l ON l.operation_id=o.operation_id WHERE o.operation_status <> 'succeeded'",
            "success_without_claim": "SELECT COUNT(*) FROM wm_inventory_operation o LEFT JOIN wm_inventory_executed_document d ON d.operation_id=o.operation_id WHERE o.operation_status='succeeded' AND d.operation_id IS NULL",
            "bad_issue_pair": "SELECT COUNT(*) FROM wm_transaction t LEFT JOIN wm_transaction s ON s.transaction_id=t.related_transaction_id WHERE t.transaction_type='ITEM_ISSUE_IN' AND (s.transaction_id IS NULL OR s.transaction_type <> 'ITEM_ISSUE_OUT' OR t.transaction_quantity <> -s.transaction_quantity OR t.source_doc_line_id <> s.source_doc_line_id OR t.source_doc_id <> s.source_doc_id)",
            "receipt_success_not_finished": "SELECT COUNT(*) FROM wm_inventory_operation o JOIN wm_item_recpt h ON h.recpt_id=o.document_id WHERE o.action_type='ITEM_RECEIPT_EXECUTE' AND o.operation_status='succeeded' AND h.status <> 'FINISHED'",
            "issue_success_not_finished": "SELECT COUNT(*) FROM wm_inventory_operation o JOIN wm_issue_header h ON h.issue_id=o.document_id WHERE o.action_type='PRODUCTION_ISSUE_EXECUTE' AND o.operation_status='succeeded' AND h.status <> 'FINISHED'",
        }
        findings = {name: scalar(cursor, sql) for name, sql in checks.items()}

        cursor.execute("SELECT operation_id,action_type,document_id FROM wm_inventory_operation WHERE operation_status='succeeded'")
        operations = cursor.fetchall()
        mismatched_operations = []
        for operation_id, action, document_id in operations:
            document_type = "IR" if action == "ITEM_RECEIPT_EXECUTE" else "ISSUE"
            line_table = "wm_item_recpt_line" if document_type == "IR" else "wm_issue_line"
            id_column = "recpt_id" if document_type == "IR" else "issue_id"
            cursor.execute(f"SELECT COUNT(*) FROM {line_table} WHERE {id_column}=%s", (document_id,))
            expected = cursor.fetchone()[0] * (1 if document_type == "IR" else 2)
            cursor.execute("SELECT transaction_id FROM wm_transaction WHERE source_doc_type=%s AND source_doc_id=%s", (document_type, document_id))
            actual = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT transaction_id FROM wm_inventory_operation_transaction WHERE operation_id=%s", (operation_id,))
            linked = {row[0] for row in cursor.fetchall()}
            if not actual or len(actual) != expected or actual != linked:
                mismatched_operations.append(operation_id)
        findings["success_transaction_mismatch"] = len(mismatched_operations)
        evidence["findings"] = findings
        evidence["pass"] = all(value == 0 for value in findings.values())
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        connection.rollback()
        return 0 if evidence["pass"] else 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
