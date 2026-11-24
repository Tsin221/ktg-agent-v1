"""Native Feishu cards for concise Agent replies and on-demand task lists."""
import re


def text(value, limit=120):
    if value is None or value == '':
        return '未记录'
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return value if len(value) <= limit else value[:limit]+'…'


def div(value):
    return {'tag':'div', 'text':{'tag':'plain_text', 'content':str(value)}}


def row(values, weights, grey=False):
    return {'tag':'column_set', 'flex_mode':'none',
            'background_style':'grey' if grey else 'default', 'horizontal_spacing':'small',
            'columns':[{'tag':'column','width':'weighted','weight':weight,
                        'vertical_align':'center','elements':[div(value)]}
                       for value, weight in zip(values, weights)]}


def task_list_elements(tasks):
    if not tasks:
        return []
    elements=[row(['编号','任务与进度'],[1,5],True)]
    for index,task in enumerate(tasks):
        description = (text(task['title'],100)+'\n已完成 '+str(task['completed'])+'/'+str(task['total'])
                       +' 步\n当前：'+text(task['current'],120))
        elements.append(row([str(task['number']),description],[1,5],index%2==1))
    return elements


def render_result(result,mes_url):
    answer=result.get('answer')
    if not isinstance(answer,str) or not answer.strip():
        answer='查询未完成，请稍后重新提问。'
    elements=[div(text(answer,180))]
    tasks=result.get('task_list')
    title='查询结果'
    if isinstance(tasks,list):
        title='任务列表'
        elements+=task_list_elements(tasks)
    elements += [
        {'tag':'action','actions':[{'tag':'button','type':'primary','text':{'tag':'plain_text','content':'打开 MES'},'url':mes_url}]},
        {'tag':'note','elements':[{'tag':'plain_text','content':'ktg_demo 演示数据'}]},
    ]
    return {'config':{'wide_screen_mode':True},
            'header':{'template':'blue','title':{'tag':'plain_text','content':'KTG · '+title}},
            'elements':elements}
