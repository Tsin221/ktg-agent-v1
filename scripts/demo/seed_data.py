"""Fictional, related business records for the dedicated experience database."""
from datetime import date, timedelta


def records():
    data = {}
    def add(table, **row):
        data.setdefault(table, []).append(row)
        return row
    today = date.today()
    day = lambda offset: str(today + timedelta(days=offset))
    add('md_unit_measure', measure_id=1, measure_code='pcs', measure_name='件', primary_flag='Y', enable_flag='Y')
    for i, name, kind in [(1, '机械零件', 'ITEM'), (2, '成品组件', 'PRODUCT')]:
        add('md_item_type', item_type_id=i, item_type_code=f'TYPE-{i}', item_type_name=name, parent_type_id=0, ancestors='0', item_or_product=kind, enable_flag='Y')
    items = []
    for i, code, name, spec, kind in [
        (1,'BRG-6204','深沟球轴承','6204 / 20×47×14 mm','ITEM'),
        (2,'BRG-6205','深沟球轴承','6205 / 25×52×15 mm','ITEM'),
        (3,'SEAL-20','轴用密封圈','20 mm','ITEM'),
        (4,'BOLT-M8','六角螺栓','M8×30','ITEM'),
        (5,'MOTOR-075','驱动电机','0.75 kW','ITEM'),
        (6,'SHAFT-20','传动轴','20 mm','ITEM'),
        (7,'SENSOR-01','接近传感器','12 V','ITEM'),
        (8,'COUPLING-20','联轴器','20 mm','ITEM'),
        (9,'ASSY-01','传动组件','标准型','PRODUCT'),
        (10,'ASSY-02','驱动组件','增强型','PRODUCT')]:
        items.append(add('md_item', item_id=i, item_code=code, item_name=name, specification=spec, unit_of_measure='pcs', item_or_product=kind, item_type_id=1 if kind=='ITEM' else 2, item_type_code='TYPE-1' if kind=='ITEM' else 'TYPE-2', item_type_name='机械零件' if kind=='ITEM' else '成品组件', enable_flag='Y', safe_stock_flag='N'))
    def item(i):
        r = items[i-1]
        return {k:r[k] for k in ('item_id','item_code','item_name','specification','unit_of_measure')}
    locations = []
    for i, code, name in [(1,'RAW','原料仓'),(2,'XBK_VIRTUAL','线边仓'),(3,'FIN','成品仓')]:
        add('wm_warehouse', warehouse_id=i, warehouse_code=code, warehouse_name=name, enable_flag='Y')
        add('wm_storage_location', location_id=i, location_code='XBKKQ_VIRTUAL' if i==2 else f'{code}-01', location_name=f'{name}一区', warehouse_id=i, area_flag='Y')
        add('wm_storage_area', area_id=i, area_code='XBKKW_VIRTUAL' if i==2 else f'{code}-A', area_name='A排', location_id=i, enable_flag='Y')
        locations.append(dict(warehouse_id=i,warehouse_code=code,warehouse_name=name,location_id=i,location_code='XBKKQ_VIRTUAL' if i==2 else f'{code}-01',location_name=f'{name}一区',area_id=i,area_code='XBKKW_VIRTUAL' if i==2 else f'{code}-A',area_name='A排'))
    stock_id=0
    for i in [1,2,3,4,5,6,8,9,10]:
        warehouse=3 if i>=9 else 1
        stock_id+=1
        add('wm_material_stock', material_stock_id=stock_id, **item(i), item_type_id=2 if i>=9 else 1, **locations[warehouse-1], batch_code='BATCH-01', quantity_onhand=0 if i==8 else (40 if i>=9 else 200), recpt_date=day(-10))
    stock_id+=1
    add('wm_material_stock',material_stock_id=stock_id,**item(1),item_type_id=1,**locations[1],batch_code='BATCH-02',quantity_onhand=30,recpt_date=day(-3))
    add('md_workshop', workshop_id=1,workshop_code='WS-01',workshop_name='装配车间',enable_flag='Y')
    add('pro_process',process_id=1,process_code='PROC-01',process_name='组件装配',enable_flag='Y')
    add('md_workstation',workstation_id=1,workstation_code='ST-01',workstation_name='装配一号工位',workshop_id=1,workshop_code='WS-01',workshop_name='装配车间',process_id=1,process_code='PROC-01',process_name='组件装配',**locations[1],enable_flag='Y')
    add('dv_machinery_type',machinery_type_id=1,machinery_type_code='ASSEMBLY',machinery_type_name='装配设备',parent_type_id=0,ancestors='0',enable_flag='Y')
    machines=[]
    for i,name,status in [(1,'一号装配机','WORKING'),(2,'二号装配机','REPAIR'),(3,'扭矩测试台','STOP')]:
        machines.append(add('dv_machinery',machinery_id=i,machinery_code=f'DEV-{i:03}',machinery_name=name,machinery_brand='星禾',machinery_spec=f'XH-{i}00',machinery_type_id=1,machinery_type_code='ASSEMBLY',machinery_type_name='装配设备',workshop_id=1,workshop_code='WS-01',workshop_name='装配车间',status=status))
    for i,name,kind,content,standard in [(1,'连接件检查','CHECK','检查连接件松动、异响','连接牢固，无异常噪声'),(2,'传动机构润滑','MAINTEN','清洁并补充传动部位润滑脂','转动顺畅，无干摩擦'),(3,'电气线路检查','CHECK','检查线路接头和绝缘','接头牢固，绝缘完好')]:
        add('dv_subject',subject_id=i,subject_code=f'SUB-{i:03}',subject_name=name,subject_type=kind,subject_content=content,subject_standard=standard,enable_flag='Y')
    for i,machine_id,status,problem in [(1,1,'FINISHED','运行时连接件异响'),(2,2,'PREPARE','启动后传动机构卡顿'),(3,1,'PREPARE','检测信号偶发中断')]:
        machine=machines[machine_id-1]
        snapshot={k:machine[k] for k in ('machinery_id','machinery_code','machinery_name','machinery_brand','machinery_spec','machinery_type_id')}
        add('dv_repair',repair_id=i,repair_code=f'REP-{i:03}',repair_name=f'{machine["machinery_name"]}维修',**snapshot,require_date=day(-7 if i==1 else -1),status=status,**(dict(finish_date=day(-6),confirm_date=day(-5),repair_result='SUCCESS') if i==1 else {}))
        subject=data['dv_subject'][0 if i<3 else 2]
        add('dv_repair_line',line_id=i,repair_id=i,**{k:v for k,v in subject.items() if k!='enable_flag'},malfunction=problem,repair_des='检查并紧固连接件，试运行无异响' if i==1 else '等待检查处理')
    for i,kind,name in [(1,'CHECK','装配设备每日点检'),(2,'MAINTEN','装配设备月度保养')]:
        add('dv_check_plan',plan_id=i,plan_code=f'PLAN-{i:03}',plan_name=name,plan_type=kind,start_date=day(-5),end_date=day(90),cycle_type='DAY' if i==1 else 'MONTH',cycle_count=1,status='FINISHED')
        machine=machines[0]
        add('dv_check_machinery',record_id=i,plan_id=i,**{k:machine[k] for k in ('machinery_id','machinery_code','machinery_name','machinery_brand','machinery_spec')})
        subject=data['dv_subject'][i-1]
        add('dv_check_subject',record_id=i,plan_id=i,**{k:v for k,v in subject.items() if k!='enable_flag'})
    add('tm_tool_type',tool_type_id=1,tool_type_code='JIG',tool_type_name='装配夹具',code_flag='Y',mainten_type='REGULAR',mainten_period=30)
    for i in range(1,4):
        add('tm_tool',tool_id=i,tool_code=f'JIG-{i:03}',tool_name=f'组件定位夹具{i}',brand='星禾',spec='标准型',tool_type_id=1,tool_type_code='JIG',tool_type_name='装配夹具',code_flag='Y',quantity=1,quantity_avail=1,mainten_type='REGULAR',next_mainten_date=day(20),status='STORE')
    for i,status,produced in [(1,'PREPARE',0),(2,'CONFIRMED',60),(3,'FINISHED',100)]:
        add('pro_workorder',workorder_id=i,workorder_code=f'WO-{i:03}',workorder_name=f'传动组件生产{i}',workorder_type='SELF',order_source='STORE',product_id=9,product_code='ASSY-01',product_name='传动组件',product_spc='标准型',unit_of_measure='pcs',quantity=100,quantity_produced=produced,quantity_scheduled=0 if i==1 else 100,request_date=day(10),parent_id=0,ancestors='0',status=status)
    for i,q in [(1,2),(3,1),(4,4)]:
        r=item(i)
        add('md_product_bom',bom_id=i,item_id=9,bom_item_id=i,bom_item_code=r['item_code'],bom_item_name=r['item_name'],bom_item_spec=r['specification'],unit_of_measure='pcs',item_or_product='ITEM',quantity=q,enable_flag='Y')
    for i,produced in [(2,60),(3,100)]:
        order=data['pro_workorder'][i-1]
        common=dict(workorder_id=i,workorder_code=order['workorder_code'],workorder_name=order['workorder_name'],workstation_id=1,workstation_code='ST-01',workstation_name='装配一号工位',process_id=1,process_code='PROC-01',process_name='组件装配',**item(9))
        add('pro_task',task_id=i,task_code=f'TASK-{i:03}',task_name='传动组件装配',**common,quantity=100,quantity_produced=produced,quantity_quanlify=produced,quantity_unquanlify=0,start_time=day(-2),end_time=day(2),duration=4,request_date=day(10),status='FINISHED' if i==3 else 'CONFIRMED')
        add('pro_feedback',record_id=i,feedback_type='SELF',task_id=i,task_code=f'TASK-{i:03}',**common,quantity=100,quantity_feedback=produced,quantity_qualified=produced,quantity_unquanlified=0,feedback_time=day(-1),status='FINISHED',user_name='demo',nick_name='体验员工',record_user='demo',record_nick='体验员工')
    add('wm_item_recpt',recpt_id=1,recpt_code='IN-001',recpt_name='轴承补货入库',**locations[0],recpt_date=day(0),status='APPROVED')
    add('wm_item_recpt_line',line_id=1,recpt_id=1,**item(1),**locations[0],quantity_recived=50,batch_code='BATCH-03')
    add('wm_issue_header',issue_id=1,issue_code='ISSUE-001',issue_name='传动组件生产领料',workorder_id=2,workorder_code='WO-002',workstation_id=1,workstation_code='ST-01',workstation_name='装配一号工位',**locations[1],issue_date=day(0),status='APPROVED')
    add('wm_issue_line',line_id=1,issue_id=1,material_stock_id=1,**item(1),**locations[0],quantity_issued=20,batch_code='BATCH-01')
    return data
