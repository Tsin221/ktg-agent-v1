<template>
  <div class="app-container">
    <el-row v-if="optType != 'view'" :gutter="10" class="mb8">
      <el-col :span="1.5">
        <el-button
          type="primary"
          plain
          icon="el-icon-plus"
          size="mini"
          @click="handleAdd"
          v-hasPermi="['mes:wm:itemrecpt:add']"
          v-if="!locationOnly"
        >新增</el-button>
      </el-col>
      <el-col :span="1.5">
        <el-button
          type="danger"
          plain
          icon="el-icon-delete"
          size="mini"
          :disabled="multiple"
          @click="handleDelete"
          v-hasPermi="['mes:wm:itemrecpt:remove']"
          v-if="!locationOnly"
        >删除</el-button>
      </el-col>
      <right-toolbar :showSearch.sync="showSearch" @queryTable="getList"></right-toolbar>
    </el-row>

    <el-table v-loading="loading" :data="itemrecptlineList" @selection-change="handleSelectionChange">
      <el-table-column type="selection" width="55" align="center" />
      <el-table-column label="物料编码" align="center" width="120px" prop="itemCode" />
      <el-table-column label="物料名称" align="center" prop="itemName" :show-overflow-tooltip="true"/>
      <el-table-column label="规格型号" align="center" prop="specification" :show-overflow-tooltip="true"/>
      <el-table-column label="单位" align="center" prop="unitName" />
      <el-table-column label="入库数量" align="center" prop="quantityRecived" />
      <el-table-column label="批次号" align="center" prop="batchCode" />
      <el-table-column label="目标仓库" align="center" prop="warehouseName" />
      <el-table-column label="目标库位" align="center" prop="locationName" />
      <el-table-column label="目标库区" align="center" prop="areaName" />
      <el-table-column label="操作" align="center" width="100px" v-if="optType != 'view'" class-name="small-padding fixed-width">
        <template slot-scope="scope">
          <el-button
            size="mini"
            type="text"
            icon="el-icon-edit"
            @click="handleUpdate(scope.row)"
            v-hasPermi="['mes:wm:itemrecpt:edit']"
          >修改</el-button>
          <el-button
            size="mini"
            type="text"
            icon="el-icon-delete"
            @click="handleDelete(scope.row)"
            v-hasPermi="['mes:wm:itemrecpt:remove']"
            v-if="!locationOnly"
          >删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <pagination
      v-show="total>0"
      :total="total"
      :page.sync="queryParams.pageNum"
      :limit.sync="queryParams.pageSize"
      @pagination="getList"
    />

    <!-- 添加或修改物料入库单行对话框 -->
    <el-dialog :title="title" :visible.sync="open" width="960px" append-to-body>
      <el-form ref="form" :model="form" :rules="rules" label-width="100px">
        <el-row>
          <el-col :span="8">
            <el-form-item label="物料编码"  prop="itemCode">
              <el-input v-model="form.itemCode" readonly="readonly" placeholder="请选择物料编码" >
                <el-button v-if="!locationOnly" slot="append" @click="handleSelectProduct" icon="el-icon-search"></el-button>
              </el-input>
            </el-form-item>
            <ItemSelect v-if="!locationOnly" ref="itemSelect" @onSelected="onItemSelected" > </ItemSelect>
          </el-col>
          <el-col :span="8">
            <el-form-item label="物料名称" prop="itemName">
              <el-input v-model="form.itemName" readonly="readonly" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="单位" prop="unitOfMeasure">
              <el-select v-model="form.unitOfMeasure" placeholder="请选择单位" @change="onUnitChanged" :disabled="locationOnly">
                <el-option
                  v-for="item in measureOptions"
                  :key="item.measureCode"
                  :label="item.measureName"
                  :value="item.measureCode"
                  :disabled="item.enableFlag == 'N' || item.measureCode !== allowedUnitOfMeasure"
                ></el-option>
              </el-select>
              <div style="color: #909399; font-size: 12px; line-height: 1.4;">库存按物料主数据单位记账，其他单位暂不支持换算。</div>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row>
          <el-col :span="8">
            <el-form-item label="入库数量" prop="quantityRecived">
              <el-input-number :min="0.01" :max="99999999" v-model="form.quantityRecived" placeholder="请输入入库数量" :disabled="locationOnly" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="生产日期期" prop="produceDate">
              <el-date-picker clearable
                v-model="form.produceDate"
                :disabled="locationOnly"
                type="date"
                value-format="yyyy-MM-dd"
                placeholder="请选择生产日期">
              </el-date-picker>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="有效期" prop="expireDate">
              <el-date-picker clearable
                v-model="form.expireDate"
                :disabled="locationOnly"
                type="date"
                value-format="yyyy-MM-dd"
                placeholder="请选择有效期">
              </el-date-picker>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row>
          <el-col :span="8">
            <el-form-item label="生产批号" prop="lotNumber">
              <el-input v-model="form.lotNumber" placeholder="请输入生产批号" :readonly="locationOnly" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="批次号" prop="batchCode">
              <el-input v-model="form.batchCode" maxlength="255" placeholder="请输入批次号" />
            </el-form-item>
          </el-col>
          <el-col :span="16">
            <el-form-item label="入库仓库" prop="warehouseId">
              <el-cascader
                v-model="warehouseInfo"
                :options="warehouseOptions"
                :props="warehouseProps"
                clearable
                placeholder="请选择仓库、库位和库区"
                @change="handleWarehouseChanged"
              />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row>
          <el-col :span="24">
            <el-form-item label="备注" prop="remark">
              <el-input v-model="form.remark" type="textarea" placeholder="请输入内容" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <div slot="footer" class="dialog-footer">
        <el-button type="primary" @click="submitForm">确 定</el-button>
        <el-button @click="cancel">取 消</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script>
import { listItemrecptline, getItemrecptline, delItemrecptline, addItemrecptline, updateItemrecptline, updateItemrecptlinePutaway } from "@/api/mes/wm/itemrecptline";
import { listAllUnitmeasure } from "@/api/mes/md/unitmeasure";
import ItemSelect  from "@/components/itemSelect/single.vue";
import IqcSelect from "@/components/iqcSelect/single.vue";
import {getTreeList} from "@/api/mes/wm/warehouse"
export default {
  name: "Itemrecptline",
  dicts: ['sys_yes_no'],
  components :{ItemSelect,IqcSelect},
  props:{
    recptId: null,
    optType: null,
    locationOnly: { type: Boolean, default: false },
    warehouseId: null,
    locationId: null,
    areaId: null
  },
  data() {
    return {
      warehouseInfo:[],
      measureOptions: [],
      allowedUnitOfMeasure: null,
      warehouseOptions:[],
      warehouseProps:{
        multiple: false,
        value: 'pId',
        label: 'pName',
      },
      // 遮罩层
      loading: true,
      // 选中数组
      ids: [],
      // 非单个禁用
      single: true,
      // 非多个禁用
      multiple: true,
      // 显示搜索条件
      showSearch: true,
      // 总条数
      total: 0,
      // 物料入库单行表格数据
      itemrecptlineList: [],
      // 弹出层标题
      title: "",
      // 是否显示弹出层
      open: false,
      // 查询参数
      queryParams: {
        pageNum: 1,
        pageSize: 10,
        recptId: this.recptId,
        itemId: null,
        itemCode: null,
        itemName: null,
        specification: null,
        unitOfMeasure: null,
        unitName: null,
        quantityRecived: null,
        batchCode: null,
        warehouseId: null,
        warehouseCode: null,
        warehouseName: null,
        locationId: null,
        locationCode: null,
        locationName: null,
        areaId: null,
        areaCode: null,
        areaName: null,
        expireDate: null,
        produceDate: null,
        lotNumber: null,
      },
      // 表单参数
      form: {},
      // 表单校验
      rules: {
        unitOfMeasure: [
          { required: true, message: "请选择单位", trigger: "change" }
        ],
        batchCode: [
          { required: true, message: "请输入批次号", trigger: "blur" },
          { max: 255, message: "批次号长度不能超过255个字符", trigger: "blur" }
        ],
        itemId: [
          { required: true, message: "产品物料ID不能为空", trigger: "blur" }
        ],
        quantityRecived: [
          { required: true, message: "入库数量不能为空", trigger: "blur" }
        ],
        warehouseId:[
          { validator: (rule, value, callback) => {
            if (!this.locationOnly && !value && !this.form.locationId && !this.form.areaId) callback();
            else if (this.form.warehouseId && this.form.locationId && this.form.areaId) callback();
            else callback(new Error("请选择完整的仓库、库位和库区"));
          }, trigger: "change" }
        ],
        itemCode: [
          { required: true, message: "物料编码不能为空", trigger: "blur" }
        ],
        remark: [
          { max: 250, message: '长度必须小于250个字符', trigger: 'blur' }
        ]
      }
    };
  },
  created() {
    this.getList();
    this.getWarehouseList();
    this.getUnitOptions();
  },
  methods: {
    /** 查询物料入库单行列表 */
    getList() {
      this.loading = true;
      listItemrecptline(this.queryParams).then(response => {
        this.itemrecptlineList = response.rows;
        this.total = response.total;
        this.loading = false;
      });
    },
    getWarehouseList(){
      getTreeList().then( response =>{
        if(response.data){
          this.warehouseOptions = response.data.filter((el) =>{
              return el.warehouseCode.indexOf('VIR') == -1;
          });;
        }
        this.warehouseOptions.map(w =>{
          w.children.map(l =>{
                  let lstr =JSON.stringify(l.children).replace(/locationId/g,'lId').replace(/areaId/g, 'pId').replace(/areaName/g,'pName');
                  l.children = JSON.parse(lstr);
          });

          let wstr = JSON.stringify(w.children).replace(/warehouseId/g,'wId').replace(/locationId/g, 'pId').replace(/locationName/g,'pName');
          w.children =  JSON.parse(wstr);

        });
        let ostr=JSON.stringify(this.warehouseOptions).replace(/warehouseId/g,'pId').replace(/warehouseName/g, 'pName');
        this.warehouseOptions = JSON.parse(ostr);
      });
    },
    getUnitOptions() {
      listAllUnitmeasure().then(response => {
        this.measureOptions = response.data || [];
        this.ensureUnitOption(this.form.unitOfMeasure, this.form.unitName);
      });
    },
    ensureUnitOption(measureCode, measureName) {
      if (!measureCode || this.measureOptions.some(item => item.measureCode === measureCode)) return;
      this.measureOptions.push({
        measureCode,
        measureName: measureName || measureCode,
        enableFlag: 'N'
      });
    },
    //选择仓库、库区、库位
    handleWarehouseChanged(obj){
      if(obj && obj.length === 3){
        this.form.warehouseId = obj[0];
        this.form.locationId = obj[1];
        this.form.areaId = obj[2];
      } else {
        this.form.warehouseId = null;
        this.form.locationId = null;
        this.form.areaId = null;
      }
      this.$nextTick(() => this.$refs.form.validateField("warehouseId"));
    },
    // 取消按钮
    cancel() {
      this.open = false;
      this.reset();
    },
    // 表单重置
    reset() {
      this.form = {
        lineId: null,
        recptId: this.recptId,
        noticeLineId: null,
        itemId: null,
        itemCode: null,
        itemName: null,
        specification: null,
        unitOfMeasure: null,
        unitName: null,
        quantityRecived: 1,
        batchId: null,
        batchCode: null,
        warehouseId: this.warehouseId,
        warehouseCode: null,
        warehouseName: null,
        locationId: this.locationId,
        locationCode: null,
        locationName: null,
        areaId: this.areaId,
        areaCode: null,
        areaName: null,
        iqcCheck: 'N',
        iqcId: null,
        iqcCode: null,
        expireDate: null,
        produceDate: null,
        lotNumber: null,
        remark: null,
        attr1: null,
        attr2: null,
        attr3: null,
        attr4: null,
        createBy: null,
        createTime: null,
        updateBy: null,
        updateTime: null
      };
      this.allowedUnitOfMeasure = null;
      this.warehouseInfo = [];
      this.resetForm("form");
    },
    /** 搜索按钮操作 */
    handleQuery() {
      this.queryParams.pageNum = 1;
      this.getList();
    },
    /** 重置按钮操作 */
    resetQuery() {
      this.resetForm("queryForm");
      this.handleQuery();
    },
    // 多选框选中数据
    handleSelectionChange(selection) {
      this.ids = selection.map(item => item.lineId)
      this.single = selection.length!==1
      this.multiple = !selection.length
    },
    /** 新增按钮操作 */
    handleAdd() {
      if (this.locationOnly) return;
      this.reset();
      this.warehouseInfo = [];
      if(this.warehouseId != null){
        let tempList = []
        tempList.push(this.warehouseId)
        tempList.push(this.locationId)
        tempList.push(this.areaId)
        this.$set(this, "warehouseInfo", tempList)
      }
      this.open = true;
      this.title = "添加物料入库单行";
    },
    /** 修改按钮操作 */
    handleUpdate(row) {
      this.reset();
      const lineId = row.lineId || this.ids
      getItemrecptline(lineId).then(response => {
        this.form = response.data;
        this.ensureUnitOption(this.form.unitOfMeasure, this.form.unitName);
        this.allowedUnitOfMeasure = this.form.unitOfMeasure;
        this.warehouseInfo = this.form.warehouseId && this.form.locationId && this.form.areaId
          ? [this.form.warehouseId, this.form.locationId, this.form.areaId]
          : [];
        this.open = true;
        this.title = "修改物料入库单行";
      });
    },
    handleSelectProduct(){
      this.$refs.itemSelect.showFlag = true;
    },
    //物料选择弹出框
    onItemSelected(obj){
        if(obj != undefined && obj != null){
          this.form.itemId = obj.itemId;
          this.form.itemCode = obj.itemCode;
          this.form.itemName = obj.itemName;
          this.form.specification = obj.specification;
          this.form.unitOfMeasure = obj.unitOfMeasure;
          this.form.unitName = obj.unitName
          this.allowedUnitOfMeasure = obj.unitOfMeasure;
          this.ensureUnitOption(this.form.unitOfMeasure, this.form.unitName);
        }
    },
    onUnitChanged(measureCode) {
      if (measureCode !== this.allowedUnitOfMeasure) {
        this.form.unitOfMeasure = this.allowedUnitOfMeasure;
        return;
      }
      const unit = this.measureOptions.find(item => item.measureCode === measureCode);
      this.form.unitName = unit ? unit.measureName : null;
    },
    //IQC检验单选择
    handleSelectIqc(){
      this.$refs.iqcSelect.showFlag = true;
    },
    //IQC检验单选择弹出框
    onIqcSelected(obj){
        if(obj != undefined && obj != null){
          this.form.iqcId = obj.iqcId;
          this.form.iqcCode = obj.iqcCode;
        }
    },
    /** 提交按钮 */
    submitForm() {
      this.$refs["form"].validate(valid => {
        if (valid) {
          if (this.form.lineId != null) {
            const update = this.locationOnly
              ? updateItemrecptlinePutaway(this.form)
              : updateItemrecptline(this.form);
            update.then(response => {
              this.$modal.msgSuccess("修改成功");
              this.open = false;
              this.getList();
              this.$emit("updated");
            });
          } else {
            addItemrecptline(this.form).then(response => {
              this.$modal.msgSuccess("新增成功");
              this.form = response.data;
              this.open = false;
              this.getList();
            });
          }
        }
      });
    },
    /** 删除按钮操作 */
    handleDelete(row) {
      const lineIds = row.lineId || this.ids;
      this.$modal.confirm('是否确认删除物料入库单行编号为"' + lineIds + '"的数据项？').then(function() {
        return delItemrecptline(lineIds);
      }).then(() => {
        this.getList();
        this.$modal.msgSuccess("删除成功");
      }).catch(() => {});
    },
    /** 导出按钮操作 */
    handleExport() {
      this.download('wm/itemrecptline/export', {
        ...this.queryParams
      }, `itemrecptline_${new Date().getTime()}.xlsx`)
    }
  }
};
</script>
