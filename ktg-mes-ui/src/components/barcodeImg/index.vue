<template>
  <div v-if="barcodeUrl" class="flex-container">
    <el-image :key="barcodeUrl" ref="image" class="barcodeClass" fit="scale-down" :src="barcodeUrl" @error="hideBarcode" />
  </div>
</template>
<script>
import { getBarcodeUrl } from "@/api/mes/wm/barcode";

export default {
  name: 'BarcodeImg',
  props: {

    bussinessId: {
      type: Number,
      default: -1
    },
    bussinessCode: {
      type: String,
      default: null
    },
    barcodeFormart: {
      type: String,
      default: 'QR_CODE'
    },
    barcodeType: {
      type: String,
      default : null
    },
  },
  data(){
    return {
      barcodeUrl: '',
      barcodeRequest: 0
    }
  },
  methods: {
    hideBarcode() {
      this.barcodeUrl = '';
    },
    async getBarcode(){
      const request = ++this.barcodeRequest;
      this.hideBarcode();
      if (!this.bussinessCode || !(Number(this.bussinessId) > 0)) {
        return;
      }
      //二维码查询参数
      let barcodeParams = {
        bussinessId: this.bussinessId,
        bussinessCode: this.bussinessCode,
        barcodeFormart: this.barcodeFormart,
        barcodeType: this.barcodeType
      };
      try {
        const response = await getBarcodeUrl(barcodeParams);
        if (request !== this.barcodeRequest) return;
        const url = response.data && response.data.barcodeUrl;
        this.barcodeUrl = typeof url === 'string' ? url.trim() : '';
      } catch (error) {
        if (request === this.barcodeRequest) this.hideBarcode();
      }
    },

  },
  watch: {
    'bussinessCode': 'getBarcode'
  }

}
</script>
<style scoped>
.barcodeClass {
  width: 200px;
  height: 200px;
  border: 1px dashed;
  position: relative;
  display: inline-block;
}

.flex-container{
  display: flex;
  justify-content: center; /* 水平居中 */
  align-items: center; /* 垂直居中 */
}
</style>
