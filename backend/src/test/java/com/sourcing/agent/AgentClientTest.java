package com.sourcing.agent;

import static org.assertj.core.api.Assertions.*;
import com.sourcing.auth.ApiException;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;

class AgentClientTest {
    private HttpServer server;
    private String base;
    private final AtomicReference<String> authorization = new AtomicReference<>();
    private final AtomicReference<String> body = new AtomicReference<>();
    private final AtomicReference<String> uri = new AtomicReference<>();
    private final AtomicReference<String> contentLength = new AtomicReference<>();
    private final AtomicReference<String> transferEncoding = new AtomicReference<>();
    private int code = 200;
    @BeforeEach void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            contentLength.set(exchange.getRequestHeaders().getFirst("Content-Length"));
            transferEncoding.set(exchange.getRequestHeaders().getFirst("Transfer-Encoding"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            uri.set(exchange.getRequestURI().toString());
            byte[] bytes = "{\"ready\":true,\"status\":\"queued\"}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(code, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        base = "http://127.0.0.1:" + server.getAddress().getPort();
    }
    @AfterEach void stop() { server.stop(0); }
    private AgentClient client() { return new AgentClient(base, "test-service-token-with-more-than-thirty-two-characters"); }
    @Test void usesBearerAndForwardsServerSelectedOwnerWithStructuredFilters() {
        var request = new AgentSearchRequest("8544", null, LocalDate.of(2025,1,1), LocalDate.of(2025,12,31), null, 5, "recent", "native", 200000, 5000);
        assertThat(client().create("buyer-one", request)).containsEntry("status", "queued");
        assertThat(authorization.get()).startsWith("Bearer test-service-token");
        assertThat(body.get()).contains("buyer-one", "hs_prefix", "8544", "scan_limit");
        assertThat(uri.get()).isEqualTo("/v1/searches");
        assertThat(Integer.parseInt(contentLength.get())).isPositive();
        assertThat(transferEncoding.get()).isNull();
    }
    @Test void readsAreScopedByOwnerAndNotAnUntrustedUrl() {
        client().get("buyer-one", "a".repeat(32));
        assertThat(uri.get()).contains("/v1/searches/" + "a".repeat(32), "owner=buyer-one");
    }
    @Test void serviceAuthenticationErrorsDoNotInvalidateTheUsersPlatformSession() {
        code = 401;
        assertThatThrownBy(() -> client().get("buyer-one", "a".repeat(32))).isInstanceOfSatisfying(ApiException.class, error -> assertThat(error.status()).isEqualTo(HttpStatus.SERVICE_UNAVAILABLE));
    }
    @Test void overloadAndMissingJobsKeepTheirMeaning() {
        for (int status : new int[]{404, 429}) {
            code = status;
            assertThatThrownBy(() -> client().get("buyer-one", "a".repeat(32))).isInstanceOfSatisfying(ApiException.class, error -> assertThat(error.status().value()).isEqualTo(status));
        }
    }
    @Test void missingTokenAndRemoteOriginsAreRejected() {
        assertThat(new AgentClient(base, "").status()).containsEntry("ready", false);
        assertThatThrownBy(() -> new AgentClient("http://example.invalid", "secret")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new AgentClient(base + "/other", "secret")).isInstanceOf(IllegalArgumentException.class);
    }
}
