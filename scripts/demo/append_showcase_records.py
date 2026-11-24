"""Generate an append-only, transaction-guarded fictional demo batch.

This generates SQL; it does not connect to any database. The SQL is deliberately
bound to ktg_demo and the reviewed existing demo anchors. --rollback can be used
for a transactional dry run before applying the identical batch.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re


BATCH = "showcase-expanded-20261121-v1"


class Variable(str):
    """Only internally assigned SQL session variables may be interpolated."""


def literal(value: object) -> str:
    if isinstance(value, Variable):
        assert re.fullmatch(r"@[a-z_][a-z_0-9]*", value)
        return str(value)
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "CONVERT(0x" + value.encode("utf-8").hex() + " USING utf8mb4)"
    raise TypeError(value)


def build(*, rollback: bool = False) -> tuple[str, Counter[str]]:
    lines = [
        "SET SESSION time_zone='+08:00';",
        "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION';",
        "CREATE TEMPORARY TABLE demo_batch_guard (ok INT NOT NULL) ENGINE=InnoDB;",
        "START TRANSACTION;",
    ]
    counts: Counter[str] = Counter()

    def guard(condition: str) -> None:
        # MySQL 5.7 ignores CHECK constraints. A strict NOT NULL insert aborts
        # the batch if a precondition or invariant fails. No --force on client.
        lines.append(f"INSERT INTO demo_batch_guard VALUES (IF(({condition}),1,NULL));")

    guard("DATABASE()='ktg_demo'")
    tables = ("md_workshop", "md_item", "md_product_bom", "dv_machinery_type", "dv_machinery", "dv_subject",
              "dv_repair", "dv_repair_line", "dv_check_plan", "dv_check_machinery", "dv_check_subject",
              "tm_tool_type", "tm_tool", "pro_workorder", "pro_workorder_bom", "pro_task", "pro_feedback",
              "wm_material_stock", "wm_item_recpt", "wm_item_recpt_line", "wm_issue_header", "wm_issue_line")
    table_list = ','.join(literal(table) for table in tables)
    guard(f"(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({table_list}) AND ENGINE='InnoDB')={len(tables)}")
    guard(f"(SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE IN ({table_list}))=0")
    for table in tables:
        guard(f"(SELECT COUNT(*) FROM {table} WHERE attr1={literal(BATCH)})=0")
        lines.append(f"SET @before_{table}=(SELECT COUNT(*) FROM {table});")

    # Existing fixed anchors are from a read-only inventory of the demo DB.
    guard("(SELECT COUNT(*) FROM md_item_type WHERE item_type_id=1 AND item_type_code='TYPE-1' AND item_or_product='ITEM')=1")
    guard("(SELECT COUNT(*) FROM md_item_type WHERE item_type_id=2 AND item_type_code='TYPE-2' AND item_or_product='PRODUCT')=1")
    guard("(SELECT COUNT(*) FROM md_unit_measure WHERE measure_code='pcs' AND enable_flag='Y')=1")
    guard("(SELECT COUNT(*) FROM md_workstation WHERE workstation_id=1 AND workstation_code='ST-01' AND process_id=1 AND process_code='PROC-01' AND enable_flag='Y')=1")
    guard("(SELECT COUNT(*) FROM pro_process WHERE process_id=1 AND process_code='PROC-01' AND enable_flag='Y')=1")
    locations = {
        'raw': dict(warehouse_id=1, warehouse_code='RAW', warehouse_name='原料仓', location_id=1,
                    location_code='RAW-01', location_name='原料仓一区', area_id=1, area_code='RAW-A', area_name='A排'),
        'line': dict(warehouse_id=2, warehouse_code='XBK_VIRTUAL', warehouse_name='线边仓', location_id=2,
                     location_code='XBKKQ_VIRTUAL', location_name='线边仓一区', area_id=2, area_code='XBKKW_VIRTUAL', area_name='A排'),
        'finished': dict(warehouse_id=3, warehouse_code='FIN', warehouse_name='成品仓', location_id=3,
                         location_code='FIN-01', location_name='成品仓一区', area_id=3, area_code='FIN-A', area_name='A排'),
    }
    for location in locations.values():
        guard("(SELECT COUNT(*) FROM wm_warehouse w JOIN wm_storage_location l ON l.warehouse_id=w.warehouse_id "
              "JOIN wm_storage_area a ON a.location_id=l.location_id WHERE " + " AND ".join([
                  f"w.warehouse_id={location['warehouse_id']}", f"w.warehouse_code={literal(location['warehouse_code'])}",
                  f"l.location_id={location['location_id']}", f"l.location_code={literal(location['location_code'])}",
                  f"a.area_id={location['area_id']}", f"a.area_code={literal(location['area_code'])}"]) + ")=1")

    def add(table: str, *, ref: str | None = None, **row: object) -> Variable | None:
        assert table in tables
        if table != 'wm_material_stock':
            row.setdefault('remark', '虚构演示数据，非真实生产记录。')
        row.update(attr1=BATCH, create_by='demo')
        for key, value in row.items():
            # Human-facing codes are unique in this added batch. The existing
            # code check also protects tables without a database unique key.
            if key in {
                'md_workshop': 'workshop_code', 'md_item': 'item_code', 'dv_machinery_type': 'machinery_type_code',
                'dv_machinery': 'machinery_code', 'dv_subject': 'subject_code', 'dv_repair': 'repair_code',
                'dv_check_plan': 'plan_code', 'tm_tool_type': 'tool_type_code', 'tm_tool': 'tool_code',
                'pro_workorder': 'workorder_code', 'pro_task': 'task_code', 'wm_item_recpt': 'recpt_code',
                'wm_issue_header': 'issue_code',
            }.get(table, '') and isinstance(value, str):
                guard(f"(SELECT COUNT(*) FROM {table} WHERE {key}={literal(value)})=0")
        cols = ','.join(row) + ',create_time'
        values = ','.join(literal(v) for v in row.values()) + ',NOW()'
        lines.append(f"INSERT INTO {table} ({cols}) VALUES ({values});")
        counts[table] += 1
        if ref:
            variable = Variable('@' + ref)
            lines.append(f"SET {literal(variable)}=LAST_INSERT_ID();")
            return variable
        return None

    def saved(table: str, ref: str, **row: object) -> Variable:
        value = add(table, ref=ref, **row)
        assert isinstance(value, Variable)
        return value

    # Master data: three distinct products and their six component materials.
    items = {}
    for key, code, name, spec, kind, low, high in [
        ('seal', 'DEMO-M-SEAL', '耐油密封组件', 'FKM / 32 mm', 'ITEM', 30, 300),
        ('valve_body', 'DEMO-M-VBODY', '阀体毛坯', '铝合金 / DN25', 'ITEM', 40, 300),
        ('guide', 'DEMO-M-GUIDE', '升降导轨', '400 mm / GH20', 'ITEM', 30, 240),
        ('motor', 'DEMO-M-LMOTOR', '升降电机', '0.55 kW / 24 V', 'ITEM', 20, 120),
        ('impeller', 'DEMO-M-IMPELLER', '循环泵叶轮', '不锈钢 / 65 mm', 'ITEM', 25, 180),
        ('gasket', 'DEMO-M-GASKET', '冷却泵密封垫', 'EPDM / 3 mm', 'ITEM', 30, 200),
        ('valve', 'DEMO-P-VALVE', '恒压阀组', 'DN25 / 标准版', 'PRODUCT', 10, 150),
        ('lift', 'DEMO-P-LIFT', '升降平台驱动组件', '额定载荷100 kg / 标准版', 'PRODUCT', 10, 100),
        ('pump', 'DEMO-P-PUMP', '冷却液循环泵组件', '65 mm / 0.75 kW', 'PRODUCT', 15, 100),
    ]:
        item_type = 1 if kind == 'ITEM' else 2
        item_id = saved('md_item', 'item_' + key, item_code=code, item_name=name, specification=spec,
                        unit_of_measure='pcs', item_or_product=kind, item_type_id=item_type,
                        item_type_code=f'TYPE-{item_type}', item_type_name='机械零件' if item_type == 1 else '成品组件',
                        enable_flag='Y', safe_stock_flag='Y', min_stock=low, max_stock=high)
        items[key] = dict(item_id=item_id, item_code=code, item_name=name, specification=spec,
                          unit_of_measure='pcs', kind=kind, type_id=item_type)

    def item_snapshot(key: str) -> dict:
        return {k: v for k, v in items[key].items() if k not in ('kind', 'type_id')}

    bom = {'valve': [('seal', 2), ('valve_body', 1)], 'lift': [('guide', 2), ('motor', 1)],
           'pump': [('impeller', 1), ('gasket', 2)]}
    for product, components in bom.items():
        for component, qty in components:
            item = items[component]
            add('md_product_bom', item_id=items[product]['item_id'], bom_item_id=item['item_id'],
                bom_item_code=item['item_code'], bom_item_name=item['item_name'], bom_item_spec=item['specification'],
                unit_of_measure='pcs', item_or_product='ITEM', quantity=qty, enable_flag='Y')

    workshops = {}
    for key, code, name, area, lead in [('mach', 'DEMO-WS-MACH', '机加车间', 360, '周主管（演示）'),
                                         ('test', 'DEMO-WS-TEST', '测试车间', 220, '赵主管（演示）')]:
        workshop_id = saved('md_workshop', 'workshop_' + key, workshop_code=code, workshop_name=name,
                            area=area, charge=lead, enable_flag='Y')
        workshops[key] = dict(workshop_id=workshop_id, workshop_code=code, workshop_name=name)

    # Devices and maintenance records: latest vs older, repaired vs pending.
    machine_types = {}
    for key, code, name in [('mach', 'DEMO-T-MACH', '机加设备'), ('test', 'DEMO-T-TEST', '测试设备')]:
        type_id = saved('dv_machinery_type', 'mtype_' + key, machinery_type_code=code, machinery_type_name=name,
                        parent_type_id=0, ancestors='0', enable_flag='Y')
        machine_types[key] = dict(machinery_type_id=type_id, machinery_type_code=code, machinery_type_name=name)
    machines = {}
    for key, code, name, spec, ws, status in [
        ('hydraulic', 'R-101', '液压成形机', 'HX-80', 'mach', 'WORKING'),
        ('lift', 'R-102', '升降平台测试台', 'LT-100', 'test', 'REPAIR'),
        ('cooling', 'C-201', '冷却液循环机', 'CL-65', 'test', 'WORKING'),
        ('drill', 'M-301', '数控钻床', 'ZK-16', 'mach', 'STOP'),
    ]:
        machine_id = saved('dv_machinery', 'machine_' + key, machinery_code=code, machinery_name=name,
                           machinery_brand='星禾（演示）', machinery_spec=spec, **machine_types[ws], **workshops[ws], status=status)
        machines[key] = dict(machinery_id=machine_id, machinery_code=code, machinery_name=name,
                             machinery_brand='星禾（演示）', machinery_spec=spec,
                             machinery_type_id=machine_types[ws]['machinery_type_id'])
    subjects = {}
    for key, code, name, kind, content, standard in [
        ('hydraulic', 'DEMO-SUB-HYD', '液压系统检查', 'CHECK', '检查油压、接头及油液泄漏情况', '油压稳定，无可见渗漏'),
        ('travel', 'DEMO-SUB-TRAVEL', '行程与限位检查', 'CHECK', '检查设备行程、限位信号与显示状态', '信号正常，行程记录一致'),
        ('cooling', 'DEMO-SUB-COOL', '冷却循环保养', 'MAINTEN', '检查过滤器、冷却液量及运行温度', '温度与液位正常，无报警'),
    ]:
        subject_id = saved('dv_subject', 'subject_' + key, subject_code=code, subject_name=name, subject_type=kind,
                           subject_content=content, subject_standard=standard, enable_flag='Y')
        subjects[key] = dict(subject_id=subject_id, subject_code=code, subject_name=name, subject_type=kind,
                             subject_content=content, subject_standard=standard)

    for key, code, name, machine, subject, reported, finish, accepted, technician, issue, resolution in [
        ('hyd_old', 'DEMO-REP-HYD-OLD', '液压成形机运行波动维修', 'hydraulic', 'hydraulic', '2026-11-05', '2026-11-06', '2026-11-07', '刘师傅（演示）', '设备运行时油压显示轻微波动。', '已完成检查，记录运行稳定。'),
        ('hyd_leak', 'DEMO-REP-HYD-LEAK', '液压成形机渗油维修', 'hydraulic', 'hydraulic', '2026-11-15', '2026-11-16', '2026-11-17', '陈工（演示）', '液压接头出现渗油，油压显示波动，现场暂停设备。', '维修记录：更换老化密封件，检查连接状态，复测油压稳定且未见渗漏。'),
        ('lift', 'DEMO-REP-LIFT-STOP', '升降平台中途停止待处理', 'lift', 'travel', '2026-11-21', None, None, None, '升降平台升到一半就停住，控制屏显示行程异常，设备暂停使用。', '仅登记故障，等待安排检修；尚未记录处理人员或完工结果。'),
        ('cool', 'DEMO-REP-COOL-HOT', '冷却循环机温度报警维修', 'cooling', 'cooling', '2026-11-18', '2026-11-19', '2026-11-20', '何师傅（演示）', '冷却液温度偏高，过滤器报警。', '维修记录：已清洁过滤器并完成试运行，温度恢复正常，报警消失。'),
    ]:
        repair_id = saved('dv_repair', 'repair_' + key, repair_code=code, repair_name=name, **machines[machine],
                          require_date=reported, finish_date=finish, confirm_date=accepted, repair_result='SUCCESS' if finish else None,
                          accepted_by=technician, confirm_by='孙班长（演示）' if accepted else None,
                          status='FINISHED' if finish else 'PREPARE')
        add('dv_repair_line', repair_id=repair_id, **subjects[subject], malfunction=issue, repair_des=resolution)

    for key, code, name, kind, cycle, machine_keys, subject_key in [
        ('mach', 'DEMO-PLAN-MACH', '机加设备每日点检', 'CHECK', 'DAY', ('hydraulic', 'drill'), 'hydraulic'),
        ('test', 'DEMO-PLAN-TEST', '测试设备月度保养', 'MAINTEN', 'MONTH', ('cooling',), 'cooling'),
    ]:
        plan = saved('dv_check_plan', 'plan_' + key, plan_code=code, plan_name=name, plan_type=kind,
                     start_date='2026-11-01', end_date='2027-02-28', cycle_type=cycle, cycle_count=1, status='FINISHED')
        for machine in machine_keys:
            add('dv_check_machinery', plan_id=plan, **{k: v for k, v in machines[machine].items() if k != 'machinery_type_id'})
        add('dv_check_subject', plan_id=plan, **subjects[subject_key])

    # Tooling: a date-based overdue/due/future contrast and usage-based records.
    tool_types = {}
    for key, code, name, mode, period in [('regular', 'DEMO-TT-POS', '精密定位夹具', 'REGULAR', 30),
                                           ('usage', 'DEMO-TT-GAUGE', '扭矩校验工装', 'USAGE', 500)]:
        type_id = saved('tm_tool_type', 'tooltype_' + key, tool_type_code=code, tool_type_name=name,
                        code_flag='Y', mainten_type=mode, mainten_period=period)
        tool_types[key] = dict(tool_type_id=type_id, tool_type_code=code, tool_type_name=name, mainten_type=mode)
    for code, name, group, spec, due, usage in [
        ('DEMO-JIG-P01', '阀体定位夹具', 'regular', 'DN25', '2026-11-18', None),
        ('DEMO-JIG-P02', '导轨定位夹具', 'regular', '400 mm', '2026-11-23', None),
        ('DEMO-JIG-P03', '叶轮定位夹具', 'regular', '65 mm', '2026-12-10', None),
        ('DEMO-GAUGE-01', '小型扭矩校验工装', 'usage', '5–25 N·m', None, 500),
        ('DEMO-GAUGE-02', '大型扭矩校验工装', 'usage', '20–100 N·m', None, 1000),
    ]:
        add('tm_tool', tool_code=code, tool_name=name, brand='星禾（演示）', spec=spec, **tool_types[group], code_flag='Y',
            quantity=1, quantity_avail=1, next_mainten_date=due, next_mainten_period=usage, status='STORE')

    # Production: different products and a traceable workorder -> BOM -> task -> feedback chain.
    station = dict(workstation_id=1, workstation_code='ST-01', workstation_name='装配一号工位',
                   process_id=1, process_code='PROC-01', process_name='组件装配')
    orders, tasks = {}, {}
    for key, code, name, product, qty, produced, status, deadline, task_status in [
        ('valve', 'DEMO-WO-VALVE', '恒压阀组备料工单', 'valve', 120, 0, 'PREPARE', '2026-11-30', None),
        ('lift', 'DEMO-WO-LIFT', '升降平台驱动组件生产', 'lift', 80, 48, 'CONFIRMED', '2026-11-27', 'CONFIRMED'),
        ('pump', 'DEMO-WO-PUMP', '冷却循环泵组件生产', 'pump', 60, 60, 'FINISHED', '2026-11-20', 'FINISHED'),
        ('lift_next', 'DEMO-WO-LIFT-02', '升降平台驱动组件第二批', 'lift', 40, 0, 'CONFIRMED', '2026-12-05', 'CONFIRMED'),
    ]:
        item = items[product]
        order_id = saved('pro_workorder', 'order_' + key, workorder_code=code, workorder_name=name, workorder_type='SELF',
                         order_source='STORE', product_id=item['item_id'], product_code=item['item_code'], product_name=item['item_name'],
                         product_spc=item['specification'], unit_of_measure='pcs', quantity=qty, quantity_produced=produced,
                         quantity_scheduled=qty if task_status else 0, request_date=deadline, parent_id=0, ancestors='0', status=status)
        orders[key] = dict(workorder_id=order_id, workorder_code=code, workorder_name=name)
        for component, per_unit in bom[product]:
            component_item = items[component]
            add('pro_workorder_bom', workorder_id=order_id, item_id=component_item['item_id'], item_code=component_item['item_code'],
                item_name=component_item['item_name'], item_spc=component_item['specification'], unit_of_measure='pcs',
                item_or_product='ITEM', quantity=qty * per_unit)
        if task_status:
            common = {**orders[key], **station, **item_snapshot(product)}
            task_code = code.replace('WO', 'TASK')
            # The actual scheduling UI treats one duration unit as eight hours.
            start, end, duration = {'pump': ('2026-11-17 08:00:00', '2026-11-19 16:00:00', 7),
                                    'lift': ('2026-11-19 08:00:00', '2026-11-23 08:00:00', 12),
                                    'lift_next': ('2026-11-24 08:00:00', '2026-11-26 16:00:00', 7)}[key]
            task_id = saved('pro_task', 'task_' + key, task_code=task_code, task_name=name + '装配', **common,
                            quantity=qty, quantity_produced=produced, quantity_quanlify=produced, quantity_unquanlify=0,
                            start_time=start, end_time=end, duration=duration, request_date=deadline, status=task_status)
            tasks[key] = dict(task_id=task_id, task_code=task_code)
            for count, day in ([(24, '2026-11-19'), (24, '2026-11-20')] if key == 'lift' else [(60, '2026-11-19')] if key == 'pump' else []):
                add('pro_feedback', **common, **tasks[key], feedback_type='SELF', quantity=qty, quantity_feedback=count,
                    quantity_qualified=count, quantity_unquanlified=0, user_name='demo', nick_name='体验员工',
                    feedback_time=day, record_user='demo', record_nick='体验员工', status='FINISHED')

    # Opening stock is separate from pending receipts and issues. Nothing is
    # marked executed and no original stock record is adjusted.
    stock = {}
    for key, warehouse, qty in [('seal', 'raw', 12), ('seal', 'line', 0), ('valve_body', 'raw', 180),
                                ('guide', 'raw', 120), ('guide', 'line', 24), ('motor', 'raw', 30), ('motor', 'line', 5),
                                ('impeller', 'raw', 95), ('gasket', 'raw', 0), ('valve', 'finished', 15),
                                ('lift', 'finished', 12), ('pump', 'finished', 28)]:
        item = items[key]
        batch_code = 'DEMO-OPEN-' + key.upper().replace('_', '-')
        ref = saved('wm_material_stock', f'stock_{key}_{warehouse}', **item_snapshot(key), item_type_id=item['type_id'],
                    **locations[warehouse], batch_code=batch_code, quantity_onhand=qty, recpt_date='2026-11-12', attr2='虚构演示期初库存')
        stock[key, warehouse] = (ref, batch_code, qty)
    for key, code, name, qty in [('seal', 'DEMO-IN-SEAL', '耐油密封组件补货入库', 100),
                                  ('gasket', 'DEMO-IN-GASKET', '冷却泵密封垫补货入库', 80)]:
        receipt = saved('wm_item_recpt', 'receipt_' + key, recpt_code=code, recpt_name=name, **locations['raw'],
                        recpt_date='2026-11-21', status='APPROVED', remark='演示预置已审核待执行，未计入现存库存。')
        add('wm_item_recpt_line', recpt_id=receipt, **item_snapshot(key), **locations['raw'],
            quantity_recived=qty, batch_code='DEMO-IN-' + key.upper())
    for key, code, name, lines_for_issue in [
        ('lift', 'DEMO-ISSUE-LIFT', '升降平台首批补充领料', [('guide', 20), ('motor', 10)]),
        ('lift_next', 'DEMO-ISSUE-LIFT-02', '升降平台第二批预备领料', [('guide', 12), ('motor', 8)]),
    ]:
        issue = saved('wm_issue_header', 'issue_' + key, issue_code=code, issue_name=name,
                      **{k: v for k, v in orders[key].items() if k != 'workorder_name'}, **tasks[key],
                      **{k: v for k, v in station.items() if k.startswith('workstation')}, **locations['line'],
                      issue_date='2026-11-21', status='APPROVED', remark='演示预置已审核待执行，尚未转移库存。')
        for component, qty in lines_for_issue:
            ref, batch_code, onhand = stock[component, 'raw']
            assert qty <= onhand
            add('wm_issue_line', issue_id=issue, material_stock_id=ref, **item_snapshot(component),
                **locations['raw'], quantity_issued=qty, batch_code=batch_code)

    # Verify exact counts, no unexpected old-row changes, and key relations before commit.
    for table in tables:
        guard(f"(SELECT COUNT(*) FROM {table} WHERE attr1={literal(BATCH)})={counts[table]}")
        guard(f"(SELECT COUNT(*) FROM {table})=@before_{table}+{counts[table]}")
        lines.append(f"SELECT {literal(table)},COUNT(*) FROM {table} WHERE attr1={literal(BATCH)};")
    tag = literal(BATCH)
    guard(f"(SELECT COUNT(*) FROM pro_workorder_bom b JOIN pro_workorder w ON w.workorder_id=b.workorder_id JOIN md_item i ON i.item_id=b.item_id JOIN md_product_bom p ON p.item_id=w.product_id AND p.bom_item_id=i.item_id WHERE b.attr1={tag} AND b.quantity=w.quantity*p.quantity AND b.item_code=i.item_code)={counts['pro_workorder_bom']}")
    guard(f"(SELECT COUNT(*) FROM pro_task t JOIN pro_workorder w ON w.workorder_id=t.workorder_id WHERE t.attr1={tag} AND t.item_id=w.product_id AND t.quantity_produced=w.quantity_produced AND t.quantity=w.quantity)={counts['pro_task']}")
    guard(f"(SELECT COUNT(*) FROM pro_workorder w WHERE w.attr1={tag} AND w.quantity_produced=COALESCE((SELECT SUM(f.quantity_feedback) FROM pro_feedback f WHERE f.workorder_id=w.workorder_id AND f.attr1={tag}),0))={counts['pro_workorder']}")
    guard(f"(SELECT COUNT(*) FROM dv_repair_line l JOIN dv_repair r ON r.repair_id=l.repair_id JOIN dv_machinery m ON m.machinery_id=r.machinery_id JOIN dv_subject s ON s.subject_id=l.subject_id WHERE l.attr1={tag} AND r.machinery_code=m.machinery_code AND l.subject_code=s.subject_code)={counts['dv_repair_line']}")
    guard(f"(SELECT COUNT(*) FROM wm_issue_line l JOIN wm_material_stock s ON s.material_stock_id=l.material_stock_id JOIN wm_issue_header h ON h.issue_id=l.issue_id JOIN pro_task t ON t.task_id=h.task_id JOIN pro_workorder w ON w.workorder_id=h.workorder_id WHERE l.attr1={tag} AND l.item_id=s.item_id AND l.warehouse_id=s.warehouse_id AND t.workorder_id=w.workorder_id AND h.status='APPROVED')={counts['wm_issue_line']}")
    guard(f"(SELECT COUNT(*) FROM (SELECT s.material_stock_id FROM wm_material_stock s JOIN wm_issue_line l ON l.material_stock_id=s.material_stock_id WHERE l.attr1={tag} GROUP BY s.material_stock_id,s.quantity_onhand HAVING SUM(l.quantity_issued)>s.quantity_onhand) violations)=0")
    guard(f"(SELECT COUNT(*) FROM wm_item_recpt_line l JOIN wm_item_recpt h ON h.recpt_id=l.recpt_id WHERE l.attr1={tag} AND h.status='APPROVED' AND NOT EXISTS (SELECT 1 FROM wm_material_stock s WHERE s.item_id=l.item_id AND s.batch_code=l.batch_code))={counts['wm_item_recpt_line']}")
    lines.append('ROLLBACK;' if rollback else 'COMMIT;')
    marker = 'DRY_RUN_ROLLED_BACK' if rollback else 'BATCH_COMMITTED'
    lines.append(f"SELECT '{marker}', {sum(counts.values())};")
    return '\n'.join(lines) + '\n', counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    sql, counts = build(rollback=args.rollback)
    args.output.write_text(sql, encoding='utf-8')
    print(dict(counts))
    print(f'Total {sum(counts.values())} records; SQL generated only.')
