package com.sourcing.agent;

import com.sourcing.auth.ApiException;
import java.net.URI;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpStatus;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.http.client.BufferingClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/** Only the trusted backend sends the owner ID and service credential. Never forwards browser URLs. */
@Service
public class AgentClient {
    private static final ParameterizedTypeReference<Map<String, Object>> MAP = new ParameterizedTypeReference<>() {};
    private final RestClient client;
    private final boolean configured;
    public AgentClient(@Value("${agent.base-url:http://127.0.0.1:8090}") String url,
                       @Value("${agent.token:}") String token) {
        URI uri = URI.create(url);
        if (!"http".equals(uri.getScheme()) || !"127.0.0.1".equals(uri.getHost()) || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null || !java.util.Set.of("", "/").contains(uri.getPath())) {
            throw new IllegalArgumentException("Agent URL must be a loopback HTTP origin");
        }
        configured = token.length() >= 32;
        var factory = new JdkClientHttpRequestFactory(HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build());
        factory.setReadTimeout(Duration.ofSeconds(5));
        client = RestClient.builder().baseUrl(url).requestFactory(new BufferingClientHttpRequestFactory(factory))
                .defaultHeader("Authorization", "Bearer " + token).build();
    }
    public Map<String, Object> status() {
        if (!configured) return Map.of("ready", false, "message", "Agent 服务尚未配置");
        try { return get("/health"); }
        catch (ApiException error) { return Map.of("ready", false, "message", error.getMessage()); }
    }
    public Map<String, Object> create(String owner, AgentSearchRequest request) {
        ensureConfigured();
        try { return response(client.post().uri("/v1/searches").body(request.payload(owner)).retrieve()); }
        catch (RestClientException error) { throw unavailable(); }
    }
    public Map<String, Object> get(String owner, String id) {
        ensureConfigured();
        try { return response(client.get().uri(builder -> builder.path("/v1/searches/{id}").queryParam("owner", owner).build(id)).retrieve()); }
        catch (RestClientException error) { throw unavailable(); }
    }
    private Map<String, Object> get(String path) {
        try { return response(client.get().uri(path).retrieve()); }
        catch (RestClientException error) { throw unavailable(); }
    }
    private Map<String, Object> response(RestClient.ResponseSpec response) {
        var result = response.onStatus(code -> code.isError(), (request, upstream) -> {
            int code = upstream.getStatusCode().value();
            if (code == 404) throw new ApiException(HttpStatus.NOT_FOUND, "调查不存在或无权访问");
            if (code == 400) throw new ApiException(HttpStatus.BAD_REQUEST, "调查条件不正确，请检查日期、商品和查询上限");
            if (code == 429) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS, "调查队列已满，请稍后重试");
            throw unavailable();
        }).body(MAP);
        if (result == null) throw unavailable();
        return result;
    }
    private void ensureConfigured() { if (!configured) throw unavailable(); }
    private static ApiException unavailable() {
        return new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务暂时不可用，请确认调查服务已启动");
    }
}
