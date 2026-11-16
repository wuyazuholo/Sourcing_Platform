package com.sourcing.agent;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.sourcing.auth.AuthService;
import com.sourcing.auth.AuthRateLimiter;
import com.sourcing.auth.SecurityConfig;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(AgentController.class)
@Import({SecurityConfig.class, AuthRateLimiter.class, AgentControllerTest.WebSecurity.class})
class AgentControllerTest {
    @TestConfiguration @EnableWebSecurity static class WebSecurity { }
    @Autowired MockMvc mvc;
    @MockitoBean AuthService auth;
    @MockitoBean AgentClient client;
    private static final String BODY = """
            {"hs":"8544","start":"2025-01-01","end":"2025-12-31","limit":5,"ranking":"recent","engine":"native","scanLimit":200000,"maxRecords":5000}
            """;
    @Test void anonymousUsersCannotAccessJobsOrStatus() throws Exception {
        mvc.perform(get("/api/agent/status")).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/agent/searches").with(csrf()).contentType(MediaType.APPLICATION_JSON).content(BODY)).andExpect(status().isUnauthorized());
        verifyNoInteractions(client);
    }
    @Test void createRequiresCsrfAndUsesAuthenticatedOwner() throws Exception {
        mvc.perform(post("/api/agent/searches").with(user("buyer-one")).contentType(MediaType.APPLICATION_JSON).content(BODY)).andExpect(status().isForbidden());
        when(client.create(eq("buyer-one"), any())).thenReturn(Map.of("id", "a".repeat(32), "status", "queued"));
        mvc.perform(post("/api/agent/searches").with(user("buyer-one")).with(csrf()).contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isAccepted()).andExpect(jsonPath("$.status").value("queued"));
        verify(auth).current("buyer-one");
        verify(client).create(eq("buyer-one"), any());
    }
    @Test void invalidFiltersAreRejectedBeforeProxying() throws Exception {
        for (String invalid : new String[]{BODY.replace("8544", "85';--"), BODY.replace("2025-01-01", "2026-01-01"), BODY.replace("200000", "2000001"), BODY.replace("\"8544\"", "null")}) {
            mvc.perform(post("/api/agent/searches").with(user("buyer-one")).with(csrf()).contentType(MediaType.APPLICATION_JSON).content(invalid)).andExpect(status().isBadRequest());
        }
        verifyNoInteractions(client);
    }
    @Test void jobReadsAlwaysUsePrincipalRegardlessOfQueryParameters() throws Exception {
        String id = "a".repeat(32);
        when(client.get("buyer-one", id)).thenReturn(Map.of("id", id, "status", "completed"));
        mvc.perform(get("/api/agent/searches/" + id + "?owner=buyer-two").with(user("buyer-one"))).andExpect(status().isOk());
        verify(client).get("buyer-one", id);
        mvc.perform(get("/api/agent/searches/not-an-id").with(user("buyer-one"))).andExpect(status().isNotFound());
    }
}
