package com.sourcing.agent;

import com.sourcing.auth.ApiException;
import com.sourcing.auth.AuthService;
import com.sourcing.auth.AuthRateLimiter;
import jakarta.validation.Valid;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/agent")
public class AgentController {
    private final AgentClient client;
    private final AuthService auth;
    private final AuthRateLimiter limiter;
    public AgentController(AgentClient client, AuthService auth, AuthRateLimiter limiter) {
        this.client = client; this.auth = auth; this.limiter = limiter;
    }
    private String owner(Authentication authentication) {
        auth.current(authentication.getName());
        return authentication.getName();
    }
    @GetMapping("/status") public Map<String, Object> status(Authentication authentication) {
        owner(authentication); return client.status();
    }
    @PostMapping("/searches") @ResponseStatus(HttpStatus.ACCEPTED)
    public Map<String, Object> create(Authentication authentication, @Valid @RequestBody AgentSearchRequest request) {
        String owner = owner(authentication);
        limiter.check("agent-search:" + owner, 12);
        return client.create(owner, request);
    }
    @GetMapping("/searches/{id}") public Map<String, Object> get(Authentication authentication, @PathVariable String id) {
        String owner = owner(authentication);
        if (!id.matches("[a-f0-9]{32}")) throw new ApiException(HttpStatus.NOT_FOUND, "调查不存在");
        return client.get(owner, id);
    }
}
