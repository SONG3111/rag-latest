package com.raglatest.backend.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/** 与原 FastAPI CORSMiddleware 同语义；开发期前端走同源代理，通常不触发。 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final RagProperties properties;

    public WebConfig(RagProperties properties) {
        this.properties = properties;
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")
                .allowedOrigins(properties.corsOrigins().toArray(String[]::new))
                .allowedMethods("*")
                .allowedHeaders("*")
                .allowCredentials(true);
    }
}
