package com.sourcing.agent;

import jakarta.validation.constraints.*;
import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.Map;

public record AgentSearchRequest(
        @Pattern(regexp="[0-9]{2,12}", message="HS 编码应为 2—12 位数字") String hs,
        @Size(min=2, max=160, message="商品描述应为 2—160 个字符") String product,
        @NotNull(message="请选择开始日期") LocalDate start,
        @NotNull(message="请选择结束日期") LocalDate end,
        @Size(min=2, max=100, message="目的国名称应为 2—100 个字符") String market,
        @NotNull @Min(1) @Max(10) Integer limit,
        @NotBlank @Pattern(regexp="recent|customer_coverage|activity") String ranking,
        @NotBlank @Pattern(regexp="native|langgraph") String engine,
        @NotNull @Min(0) @Max(2000000) Integer scanLimit,
        @NotNull @Min(1) @Max(50000) Integer maxRecords) {
    @AssertTrue(message="请填写 HS 或商品描述，并确认起止日期及结束日期范围")
    public boolean isQueryValid() {
        return (hs != null && !hs.isBlank() || product != null && !product.isBlank())
                && start != null && end != null && !start.isAfter(end) && end.isBefore(LocalDate.of(9999, 12, 31));
    }
    Map<String, Object> payload(String owner) {
        var requirements = new LinkedHashMap<String, Object>();
        if (hs != null) requirements.put("hs_prefix", hs);
        if (product != null) requirements.put("product_text", product);
        if (market != null) requirements.put("market_country", market);
        requirements.put("start_date", start.toString());
        requirements.put("end_date", end.toString());
        requirements.put("limit", limit);
        requirements.put("ranking", ranking);
        return Map.of("owner", owner, "requirements", requirements, "engine", engine, "scan_limit", scanLimit, "max_records", maxRecords);
    }
}
