"""Compact native Feishu tables and recorded timelines; no model-made facts."""
import re

STATUS = {'PREPARE': '草稿', 'CONFIRMED': '已确认', 'APPROVED': '已审核',
          'FINISHED': '已完成', 'CANCELED': '已取消', 'WORKING': '生产中',
          'STOP': '停机', 'REPAIR': '维修中'}
LABELS = {'repairCode':'维修单', 'machineryCode':'设备编码', 'machineryName':'设备',
          'workorderCode':'工单', 'workorderName':'名称', 'itemCode':'物料编码',
          'itemName':'物料', 'warehouseName':'仓库', 'quantity':'数量',
          'availableQuantity':'可用量', 'unitName':'单位', 'status':'状态',
          'toolCode':'工装编码', 'toolName':'工装', 'checkplanCode':'计划', 'checkplanName':'名称'}
FLOW_LABELS = {'dv_repair_list':'定位维修单', 'dv_repair_detail':'读取单头',
               'dv_repair_lines':'读取维修内容', 'dv_machinery_list':'查找设备',
               'dv_machinery_detail':'读取设备', 'workorder_list':'查询工单'}


def text(value, limit=120):
    if value is None or value == '':
        return '未记录'
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return value if len(value) <= limit else value[:limit]+'…'


def div(value):
    return {'tag':'div', 'text':{'tag':'plain_text', 'content':str(value)}}


def row(values, weights=None, grey=False):
    weights = weights or [1]*len(values)
    return {'tag':'column_set', 'flex_mode':'none',
            'background_style':'grey' if grey else 'default', 'horizontal_spacing':'small',
            'columns':[{'tag':'column','width':'weighted','weight':weight,
                        'vertical_align':'center','elements':[div(value)]}
                       for value, weight in zip(values, weights)]}


def table(headers, rows, weights=None):
    return [row(headers, weights, True)]+[row(values, weights, n%2==1) for n,values in enumerate(rows)]


def flow(nodes):
    """Native columns form labelled nodes separated by arrows, not Mermaid text."""
    columns=[]
    for node in nodes[:3]:
        if columns:
            columns.append({'tag':'column','width':'auto','vertical_align':'center','elements':[div('→')]})
        columns.append({'tag':'column','width':'weighted','weight':1,
                        'vertical_align':'center','elements':[div(node)]})
    return {'tag':'column_set','flex_mode':'none','background_style':'grey',
            'horizontal_spacing':'small','columns':columns}


def recorded_flow(repair):
    nodes=[]
    for label,key in [('报修','requireDate'),('完工','finishDate'),('验收','confirmDate')]:
        value=repair.get(key)
        if isinstance(value,str) and re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[ T])',value):
            nodes.append(label+'\n'+value[:10])
    return flow(nodes) if len(nodes)>=2 else None


def repair_view(details):
    repairs={}
    for item in details:
        if item.get('kind') in ('dv_repair_detail','dv_repair_lines'):
            repair=item.get('repair')
            if isinstance(repair,dict) and isinstance(repair.get('repairId'),int):
                repairs[repair['repairId']]=repair
    if len(repairs)!=1:
        return None
    rid,repair=next(iter(repairs.items()))
    code=text(repair.get('repairCode'),40)
    state=STATUS.get(repair.get('status'),text(repair.get('status'),20))
    header=code+' · '+state
    device=' · '.join(text(repair[k],60) for k in ('machineryCode','machineryName') if repair.get(k)) or '未记录'
    outcome={'SUCCESS':'成功','FAIL':'失败','FAILED':'失败'}.get(repair.get('repairResult'),text(repair.get('repairResult'),30))
    facts=[['设备',device],['结果',outcome]]
    line_results=[d for d in details if d.get('kind')=='dv_repair_lines'
                  and isinstance(d.get('repair'),dict) and d['repair'].get('repairId')==rid]
    lines=[];total=0
    for d in line_results:
        for item in d.get('rows',[]) if isinstance(d.get('rows'),list) else []:
            if isinstance(item,dict) and item.get('repairId')==rid and item not in lines:
                lines.append(item)
        if isinstance(d.get('total'),int):total=max(total,d['total'])
    notes=[]
    if not line_results:
        facts.extend([['故障','未查询'],['处理','未查询']])
    elif not lines:
        facts.append(['明细','无明细' if total==0 else '本页未返回明细'])
    elif len(lines)==1:
        facts.extend([['故障',text(lines[0].get('malfunction'))],['处理',text(lines[0].get('repairDes'))]])
    elements=table(['项目','信息'],facts,[1,4])
    if len(lines)>1:
        elements+=table(['故障','处理'],[[text(d.get('malfunction'),80),text(d.get('repairDes'),80)] for d in lines[:3]])
    if total>len(lines) or len(lines)>3:
        notes.append(f'明细显示 {min(len(lines),3)}/{total or len(lines)} 条')
    timeline=recorded_flow(repair)
    if timeline:elements += [div('记录日期'),timeline]
    return header,elements,notes


def list_view(details):
    for item in reversed(details):
        rows=item.get('rows')
        if not isinstance(rows,list):continue
        rows=[r for r in rows if isinstance(r,dict)]
        kind=item.get('kind','')
        if kind=='dv_repair_list':fields=['repairCode','machineryCode','status']
        elif kind=='dv_machinery_list':fields=['machineryCode','machineryName','status']
        else:fields=[k for k in LABELS if any(k in r for r in rows)][:4]
        total=item.get('total')
        count=total if isinstance(total,int) and total>=len(rows) else len(rows)
        if not rows and count==0:
            return '未找到记录',[div('当前查询范围内无匹配记录。')],[]
        if not fields or not rows:continue
        values=[]
        for r in rows[:5]:
            values.append([STATUS.get(r.get(k),text(r.get(k),70)) if k=='status' else text(r.get(k),70) for k in fields])
        notes=[f'显示 {min(len(rows),5)}/{count} 条']
        return '查询结果',table([LABELS[k] for k in fields],values),notes
    return None


def concise_answer(answer):
    if not isinstance(answer,str) or not answer.strip():
        return [div('查询未完成，请稍后重新提问。')]
    fields=[]
    for line in answer.splitlines():
        line=re.sub(r'^\s*[-*#\d.、]+\s*','',line).strip()
        match=re.match(r'^([^：:]{1,12})[：:]\s*(.+)$',line)
        if match:fields.append([match[1],text(match[2],100)])
    if fields:
        return table(['项目','信息'],fields[:5],[1,4])
    return table(['结果'],[[text(answer,180)]])


def render_result(result,mes_url):
    details=[d for d in result.get('results',[]) if isinstance(d,dict)] if isinstance(result.get('results'),list) else []
    preview=any('preview' in str(d.get('kind','')) or d.get('outcome')=='prepared' for d in details)
    view=None if preview else (repair_view(details) or list_view(details))
    if view:
        title,elements,notes=view
    else:
        title='待确认预览' if preview else '查询结果'
        elements=concise_answer(result.get('answer'))
        notes=['仅预览，未提交'] if preview else []
        nodes=[]
        for item in details:
            label=FLOW_LABELS.get(item.get('kind'))
            if label and (not nodes or nodes[-1]!=label):nodes.append(label)
        if len(nodes)>=2:elements.append(flow(nodes))
    if result.get('error_code') or any(d.get('error_code') or d.get('outcome')=='failed' for d in details):
        notes.append('部分查询未完成')
    elements += [
        {'tag':'action','actions':[{'tag':'button','type':'primary','text':{'tag':'plain_text','content':'打开 MES'},'url':mes_url}]},
        {'tag':'note','elements':[{'tag':'plain_text','content':' · '.join(notes+['ktg_demo 演示数据'])}]},
    ]
    return {'config':{'wide_screen_mode':True},
            'header':{'template':'blue','title':{'tag':'plain_text','content':'KTG · '+title}},
            'elements':elements}
