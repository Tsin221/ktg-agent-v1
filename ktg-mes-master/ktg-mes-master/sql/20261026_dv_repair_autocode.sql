-- Restore the repair work-order auto-code rule only where it is missing.
-- Existing rule definitions, parts, generated results, and sequence state are left untouched.
-- Rule IDs are resolved from rule_code so this migration does not depend on initialization IDs.

INSERT INTO sys_auto_code_rule (
    rule_code, rule_name, rule_desc, max_length, is_padded, padded_method,
    enable_flag, create_by, create_time, update_by, update_time
)
SELECT
    'REPAIR_CODE', '维修工单编号规则', NULL, 10, 'N', 'L',
    'Y', 'admin', NOW(), '', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM sys_auto_code_rule WHERE rule_code = 'REPAIR_CODE'
);

INSERT INTO sys_auto_code_part (
    rule_id, part_index, part_type, part_code, part_name, part_length,
    fix_character, create_by, create_time, update_by, update_time
)
SELECT acr.rule_id, 1, 'FIXCHAR', 'PREFIX', '前缀', 3,
       'REP', 'admin', NOW(), '', NULL
FROM sys_auto_code_rule acr
WHERE acr.rule_code = 'REPAIR_CODE'
  AND NOT EXISTS (
      SELECT 1 FROM sys_auto_code_part acp
      WHERE acp.rule_id = acr.rule_id AND acp.part_index = 1
  );

INSERT INTO sys_auto_code_part (
    rule_id, part_index, part_type, part_code, part_name, part_length,
    date_format, create_by, create_time, update_by, update_time
)
SELECT acr.rule_id, 2, 'NOWDATE', 'DATE', '年份', 4,
       'yyyy', 'admin', NOW(), '', NULL
FROM sys_auto_code_rule acr
WHERE acr.rule_code = 'REPAIR_CODE'
  AND NOT EXISTS (
      SELECT 1 FROM sys_auto_code_part acp
      WHERE acp.rule_id = acr.rule_id AND acp.part_index = 2
  );

INSERT INTO sys_auto_code_part (
    rule_id, part_index, part_type, part_code, part_name, part_length,
    seria_start_no, seria_step, cycle_flag, cycle_method,
    create_by, create_time, update_by, update_time
)
SELECT acr.rule_id, 3, 'SERIALNO', 'SERIAL', '流水号', 3,
       1, 1, 'Y', 'YEAR', 'admin', NOW(), '', NULL
FROM sys_auto_code_rule acr
WHERE acr.rule_code = 'REPAIR_CODE'
  AND NOT EXISTS (
      SELECT 1 FROM sys_auto_code_part acp
      WHERE acp.rule_id = acr.rule_id AND acp.part_index = 3
  );
