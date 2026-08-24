package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	assetRoot   = "/usr/share/ostiari"
	defaultPort = "9000"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = defaultPort
	}

	if len(os.Args) == 2 && os.Args[1] == "--healthcheck" {
		if err := healthcheck(port); err != nil {
			log.Printf("healthcheck failed: %v", err)
			os.Exit(1)
		}
		return
	}

	logger := log.New(os.Stdout, "frontend: ", log.LstdFlags|log.LUTC)
	handler, err := staticHandler(assetRoot)
	if err != nil {
		logger.Fatal(err)
	}

	server := &http.Server{
		Addr:              net.JoinHostPort("", port),
		Handler:           securityHeaders(handler),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
		ErrorLog:          logger,
	}

	ctx, stop := signal.NotifyContext(
		context.Background(),
		syscall.SIGINT,
		syscall.SIGTERM,
	)
	defer stop()

	errs := make(chan error, 1)
	go func() {
		logger.Printf("serving dashboard on %s", server.Addr)
		errs <- server.ListenAndServe()
	}()

	select {
	case err := <-errs:
		if !errors.Is(err, http.ErrServerClosed) {
			logger.Fatal(err)
		}
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Printf("graceful shutdown failed: %v", err)
			if closeErr := server.Close(); closeErr != nil {
				logger.Printf("forced shutdown failed: %v", closeErr)
			}
		}
	}
}

func staticHandler(root string) (http.Handler, error) {
	info, err := os.Stat(filepath.Join(root, "index.html"))
	if err != nil {
		return nil, fmt.Errorf("dashboard index is unavailable: %w", err)
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("dashboard index is not a regular file")
	}

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				w.Header().Set("Allow", "GET, HEAD")
				http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			if r.Method == http.MethodGet {
				_, _ = io.WriteString(w, `{"status":"healthy"}`)
			}
			return
		}

		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		relative := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		candidate := filepath.Join(root, filepath.FromSlash(relative))
		if candidate != root &&
			!strings.HasPrefix(candidate, root+string(os.PathSeparator)) {
			http.NotFound(w, r)
			return
		}

		if relative != "" {
			if served := serveRegularFile(w, r, candidate, relative); served {
				return
			}
			if strings.HasPrefix(relative, "assets/") || path.Ext(relative) != "" {
				w.Header().Set("Cache-Control", "no-store")
				http.NotFound(w, r)
				return
			}
		}

		w.Header().Set("Cache-Control", "no-store")
		serveRegularFile(w, r, filepath.Join(root, "index.html"), "index.html")
	}), nil
}

func serveRegularFile(
	w http.ResponseWriter,
	r *http.Request,
	filename string,
	requestPath string,
) bool {
	file, err := os.Open(filename)
	if err != nil {
		return false
	}
	defer file.Close()

	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return false
	}

	if strings.HasPrefix(requestPath, "assets/") {
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	} else {
		w.Header().Set("Cache-Control", "no-cache")
	}
	http.ServeContent(w, r, info.Name(), info.ModTime(), file)
	return true
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		next.ServeHTTP(w, r)
	})
}

func healthcheck(port string) error {
	client := &http.Client{
		Timeout: 4 * time.Second,
		Transport: &http.Transport{
			Proxy: nil,
		},
	}
	url := "http://" + net.JoinHostPort("127.0.0.1", port) + "/health"
	request, err := http.NewRequestWithContext(
		context.Background(),
		http.MethodGet,
		url,
		nil,
	)
	if err != nil {
		return err
	}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status %s", response.Status)
	}
	return nil
}
