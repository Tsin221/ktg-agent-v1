package com.ktg.mes.pro.controller.dto;

import java.math.BigDecimal;
import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.Date;
import javax.validation.constraints.DecimalMin;
import javax.validation.constraints.NotBlank;
import javax.validation.constraints.NotNull;
import javax.validation.constraints.Pattern;
import javax.validation.constraints.PositiveOrZero;
import javax.validation.constraints.Size;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.ktg.common.exception.ServiceException;
import com.ktg.mes.pro.domain.ProWorkorder;

/**
 * 创建或完整编辑生产工单草稿时允许客户端提交的字段。
 * 未列出的产品快照、状态、进度和审计字段会被明确忽略。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class ProWorkorderDraftRequest
{
    @PositiveOrZero(message = "workorderId: 工单ID不能为负数")
    private Long workorderId;

    @PositiveOrZero(message = "version: 版本号不能为负数")
    private Long version;

    @NotBlank(message = "workorderCode: 工单编号不能为空")
    @Size(max = 64, message = "workorderCode: 工单编号长度不能超过64个字符")
    private String workorderCode;

    @NotBlank(message = "workorderName: 工单名称不能为空")
    @Size(max = 255, message = "workorderName: 工单名称长度不能超过255个字符")
    private String workorderName;

    @NotBlank(message = "workorderType: 工单类型不能为空")
    @Pattern(regexp = "SELF|OUTSOURCE|PURCHASE",
            message = "workorderType: 工单类型必须是 SELF、OUTSOURCE 或 PURCHASE")
    private String workorderType;

    @NotBlank(message = "orderSource: 来源类型不能为空")
    @Size(max = 64, message = "orderSource: 来源类型长度不能超过64个字符")
    private String orderSource;

    @Size(max = 64, message = "sourceCode: 来源单据长度不能超过64个字符")
    private String sourceCode;

    @NotNull(message = "productId: 产品不能为空")
    @PositiveOrZero(message = "productId: 产品ID不能为负数")
    private Long productId;

    @Size(max = 64, message = "batchCode: 批次号长度不能超过64个字符")
    private String batchCode;

    @NotNull(message = "quantity: 生产数量不能为空")
    @DecimalMin(value = "0", inclusive = false, message = "quantity: 生产数量必须大于0")
    private BigDecimal quantity;

    @PositiveOrZero(message = "clientId: 客户ID不能为负数")
    private Long clientId;

    @Size(max = 64, message = "clientCode: 客户编码长度不能超过64个字符")
    private String clientCode;

    @Size(max = 255, message = "clientName: 客户名称长度不能超过255个字符")
    private String clientName;

    @NotBlank(message = "requestDate: 需求日期不能为空")
    @Pattern(regexp = "\\d{4}-\\d{2}-\\d{2}", message = "requestDate: 需求日期格式必须为yyyy-MM-dd")
    private String requestDate;

    @PositiveOrZero(message = "parentId: 父工单ID不能为负数")
    private Long parentId;

    @Size(max = 500, message = "remark: 备注长度不能超过500个字符")
    private String remark;

    public ProWorkorder toWorkorder()
    {
        ProWorkorder workorder = new ProWorkorder();
        workorder.setWorkorderId(workorderId);
        workorder.setVersion(version);
        workorder.setWorkorderCode(workorderCode);
        workorder.setWorkorderName(workorderName);
        workorder.setWorkorderType(workorderType);
        workorder.setOrderSource(orderSource);
        workorder.setSourceCode(sourceCode);
        workorder.setProductId(productId);
        workorder.setBatchCode(batchCode);
        workorder.setQuantity(quantity);
        workorder.setClientId(clientId);
        workorder.setClientCode(clientCode);
        workorder.setClientName(clientName);
        workorder.setRequestDate(parseRequestDate());
        workorder.setParentId(parentId);
        workorder.setRemark(remark);
        return workorder;
    }

    private Date parseRequestDate()
    {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd");
        format.setLenient(false);
        try
        {
            return format.parse(requestDate);
        }
        catch (ParseException e)
        {
            throw new ServiceException("requestDate: 需求日期不是有效日期");
        }
    }

    public Long getWorkorderId() { return workorderId; }
    public void setWorkorderId(Long workorderId) { this.workorderId = workorderId; }
    public Long getVersion() { return version; }
    public void setVersion(Long version) { this.version = version; }
    public String getWorkorderCode() { return workorderCode; }
    public void setWorkorderCode(String workorderCode) { this.workorderCode = workorderCode; }
    public String getWorkorderName() { return workorderName; }
    public void setWorkorderName(String workorderName) { this.workorderName = workorderName; }
    public String getWorkorderType() { return workorderType; }
    public void setWorkorderType(String workorderType) { this.workorderType = workorderType; }
    public String getOrderSource() { return orderSource; }
    public void setOrderSource(String orderSource) { this.orderSource = orderSource; }
    public String getSourceCode() { return sourceCode; }
    public void setSourceCode(String sourceCode) { this.sourceCode = sourceCode; }
    public Long getProductId() { return productId; }
    public void setProductId(Long productId) { this.productId = productId; }
    public String getBatchCode() { return batchCode; }
    public void setBatchCode(String batchCode) { this.batchCode = batchCode; }
    public BigDecimal getQuantity() { return quantity; }
    public void setQuantity(BigDecimal quantity) { this.quantity = quantity; }
    public Long getClientId() { return clientId; }
    public void setClientId(Long clientId) { this.clientId = clientId; }
    public String getClientCode() { return clientCode; }
    public void setClientCode(String clientCode) { this.clientCode = clientCode; }
    public String getClientName() { return clientName; }
    public void setClientName(String clientName) { this.clientName = clientName; }
    public String getRequestDate() { return requestDate; }
    public void setRequestDate(String requestDate) { this.requestDate = requestDate; }
    public Long getParentId() { return parentId; }
    public void setParentId(Long parentId) { this.parentId = parentId; }
    public String getRemark() { return remark; }
    public void setRemark(String remark) { this.remark = remark; }
}
