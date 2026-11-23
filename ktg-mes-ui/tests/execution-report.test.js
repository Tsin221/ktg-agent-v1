const assert = require('assert')
const fs = require('fs')
const vm = require('vm')
const load = file => {
  const source = fs.readFileSync(file, 'utf8').replace(/export (function|class|const)/g, '$1')
  const context = { module: {}, crypto: { randomUUID: () => 'be6b6da3-2762-42d6-9821-4316d7e35264' } }
  vm.runInNewContext(source + '\nmodule.exports={validateReport, pendingKey, ExecutionCommand};', context)
  return context.module.exports
}
const { validateReport, pendingKey, ExecutionCommand } = load('src/views/mes/pro/execution/reportContract.js')
const selected = Array.from({length:10}, (_, i) => ({snId:i+1,snCode:'L'+i}))
const stock = (materialStockId, sourceBatchCode, quantityOnhand) => ({materialStockId, sourceBatchCode, quantityOnhand, itemCode:'BAT', unitOfMeasure:'个'})
const groups = [
  {stock:stock(51,'B06','2.00'),snIds:[1,2],quantityPerUnit:'1.00'},
  {stock:stock(52,'B07','3.00'),snIds:[3,4,5],quantityPerUnit:'1'},
  {stock:stock(53,'B08','5.00'),snIds:[6,7,8,9,10],quantityPerUnit:'1'}
]
const valid = validateReport(selected,groups)
assert.strictEqual(valid.error,'')
assert.deepStrictEqual(JSON.parse(JSON.stringify(valid.payload)),{snIds:[1,2,3,4,5,6,7,8,9,10],inputs:groups.map(g=>({snIds:g.snIds,materialStockId:g.stock.materialStockId,quantityPerUnit:g.quantityPerUnit}))})
assert.strictEqual(valid.totals[1].quantity,'3.00')
assert(validateReport(selected,[...groups,{...groups[1],snIds:[4]}]).error,'duplicate SN/stock is not a valid report')
assert(validateReport(selected,[...groups.slice(0,1),{...groups[1],quantityPerUnit:'1.01'},groups[2]]).error,'stock aggregate is exact')
assert(validateReport(selected,[...groups.slice(0,1),{...groups[1],quantityPerUnit:'0.001'},groups[2]]).error,'sub-cent precision is rejected')
assert(validateReport(selected,groups.slice(0,2)).error,'every selected SN must have inputs')
assert(validateReport(selected,[...groups.slice(0,2),{...groups[2],snIds:[6,7,8,9,10,99]}]).error,'foreign SN cannot enter report')
assert(validateReport([selected[0],selected[0]],groups).error,'duplicate selection is rejected')
const values={}
const storage={getItem:key=>values[key]||null,setItem:(key,v)=>{values[key]=v},removeItem:key=>delete values[key]}
let writes=0,reads=0,mode='unknown',original=[]
const commands={report:async (id,payload)=>{writes++;original.push(JSON.stringify([id,payload]));if(mode==='unknown')throw Error('Network error');if(mode==='invalid')throw Object.assign(Error('余额不足'),{data:{errorCode:'INSUFFICIENT_STOCK'}});return {code:200,data:{operationId:payload.operationId,resultRef:'{"snCount":10}'}}}}
const lookup=async id=>{reads++;if(mode==='receiptMissing')throw Object.assign(Error('not found'),{data:{errorCode:'NOT_FOUND'}});return {code:200,data:{operationId:id,resultRef:'{"snCount":10}'}}}
;(async()=>{
 const cmd=new ExecutionCommand(storage, pendingKey(19), commands,lookup)
 const result=await cmd.begin('report',101,valid.payload)
 assert(result.pending);assert.strictEqual(writes,1)
 assert.strictEqual(pendingKey(19)===pendingKey(20),false)
 const restored=new ExecutionCommand(storage,pendingKey(19),commands,lookup)
 assert.strictEqual(restored.pending().payload.operationId,result.pending.payload.operationId)
 await restored.begin('report',101,valid.payload);assert.strictEqual(writes,1)
 mode='receiptMissing';assert((await restored.recover()).retryable);assert.strictEqual(reads,1)
 mode='success';assert((await restored.retry()).response);assert.strictEqual(writes,2);assert.strictEqual(original[0],original[1]);assert.strictEqual(restored.pending(),null)
 mode='invalid';assert(!(await restored.begin('report',101,valid.payload)).pending);assert.strictEqual(restored.pending(),null)
 mode='unknown';await restored.begin('report',101,valid.payload);mode='success';assert((await restored.recover()).response);assert.strictEqual(restored.pending(),null)
 console.log('Execution report grouping, exact stock totals, validation, durable unknown result and same-payload recovery passed')
})().catch(error=>{console.error(error);process.exit(1)})
