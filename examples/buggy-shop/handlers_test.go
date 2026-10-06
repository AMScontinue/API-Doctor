package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func request(t *testing.T, handler http.Handler, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var payload bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&payload).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, &payload)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	return response
}

func decode[T any](t *testing.T, response *httptest.ResponseRecorder) T {
	t.Helper()
	var value T
	if err := json.NewDecoder(response.Body).Decode(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func TestUserLifecycle(t *testing.T) {
	handler := newHandler(newStore(), config{})
	created := request(t, handler, http.MethodPost, "/users", map[string]any{
		"name": "Ada", "email": "ada@example.test",
	})
	if created.Code != http.StatusCreated {
		t.Fatalf("create status = %d", created.Code)
	}
	value := decode[user](t, created)

	found := request(t, handler, http.MethodGet, "/users/1", nil)
	if found.Code != http.StatusOK || decode[user](t, found).ID != value.ID {
		t.Fatalf("get user failed: status=%d", found.Code)
	}
	deleted := request(t, handler, http.MethodDelete, "/users/1", nil)
	if deleted.Code != http.StatusNoContent {
		t.Fatalf("delete status = %d", deleted.Code)
	}
	missing := request(t, handler, http.MethodGet, "/users/1", nil)
	if missing.Code != http.StatusNotFound {
		t.Fatalf("missing status = %d", missing.Code)
	}
}

func TestOrderLifecycle(t *testing.T) {
	handler := newHandler(newStore(), config{})
	created := request(t, handler, http.MethodPost, "/orders", map[string]any{
		"product_id": 1, "quantity": 2, "total_amount": 20,
		"discount_amount": 2, "start_time": "2026-10-02T10:00:00Z",
		"end_time": "2026-10-02T11:00:00Z", "payment_method": "cash",
	})
	if created.Code != http.StatusCreated {
		t.Fatalf("create status = %d body=%s", created.Code, created.Body.String())
	}
	value := decode[order](t, created)
	if value.ID != 1 || value.Status != "created" {
		t.Fatalf("created order = %#v", value)
	}
	found := request(t, handler, http.MethodGet, "/orders/1", nil)
	if found.Code != http.StatusOK || decode[order](t, found).ID != 1 {
		t.Fatalf("get order failed: status=%d", found.Code)
	}
}

func TestUserEmailMustBeUnique(t *testing.T) {
	handler := newHandler(newStore(), config{})
	body := map[string]string{"name": "Ada", "email": "ada@example.test"}
	if response := request(t, handler, http.MethodPost, "/users", body); response.Code != http.StatusCreated {
		t.Fatalf("first create status = %d", response.Code)
	}
	if response := request(t, handler, http.MethodPost, "/users", body); response.Code != http.StatusConflict {
		t.Fatalf("duplicate create status = %d", response.Code)
	}
}

func TestOrderStatusTransitions(t *testing.T) {
	handler := newHandler(newStore(), config{})
	created := request(t, handler, http.MethodPost, "/orders", map[string]any{
		"product_id": 1, "quantity": 2, "total_amount": 20,
		"discount_amount": 2, "start_time": "2026-10-02T10:00:00Z",
		"end_time": "2026-10-02T11:00:00Z", "payment_method": "cash",
	})
	if created.Code != http.StatusCreated {
		t.Fatalf("create status = %d", created.Code)
	}
	confirmed := request(t, handler, http.MethodPatch, "/orders/1", map[string]string{
		"status": "confirmed",
	})
	if confirmed.Code != http.StatusOK || decode[order](t, confirmed).Status != "confirmed" {
		t.Fatalf("confirm failed: status=%d", confirmed.Code)
	}
	repeated := request(t, handler, http.MethodPatch, "/orders/1", map[string]string{
		"status": "confirmed",
	})
	if repeated.Code != http.StatusConflict {
		t.Fatalf("repeated confirm status = %d", repeated.Code)
	}
	cancelled := request(t, handler, http.MethodPatch, "/orders/1", map[string]string{
		"status": "cancelled",
	})
	if cancelled.Code != http.StatusOK || decode[order](t, cancelled).Status != "cancelled" {
		t.Fatalf("cancel failed: status=%d", cancelled.Code)
	}
	terminal := request(t, handler, http.MethodPatch, "/orders/1", map[string]string{
		"status": "confirmed",
	})
	if terminal.Code != http.StatusConflict {
		t.Fatalf("terminal transition status = %d", terminal.Code)
	}
}

func TestDefectsAreExplicitlyConfigurable(t *testing.T) {
	clean := newHandler(newStore(), config{})
	invalid := request(t, clean, http.MethodPost, "/users", map[string]string{
		"name": "Ada", "email": "not-an-email",
	})
	if invalid.Code != http.StatusUnprocessableEntity {
		t.Fatalf("clean invalid email status = %d", invalid.Code)
	}

	buggy := newHandler(newStore(), config{acceptInvalidEmail: true, unknownUserReturns200: true})
	accepted := request(t, buggy, http.MethodPost, "/users", map[string]string{
		"name": "Ada", "email": "not-an-email",
	})
	if accepted.Code != http.StatusCreated {
		t.Fatalf("buggy invalid email status = %d", accepted.Code)
	}
	unknown := request(t, buggy, http.MethodGet, "/users/999", nil)
	if unknown.Code != http.StatusOK {
		t.Fatalf("buggy unknown user status = %d", unknown.Code)
	}
}

func TestStatefulDefectsAreExplicitlyConfigurable(t *testing.T) {
	data := newStore()
	handler := newHandler(data, config{
		acceptDuplicateEmail:   true,
		confirmReturnsConflict: true,
	})
	body := map[string]string{"name": "Ada", "email": "ada@example.test"}
	request(t, handler, http.MethodPost, "/users", body)
	if response := request(t, handler, http.MethodPost, "/users", body); response.Code != http.StatusCreated {
		t.Fatalf("duplicate defect status = %d", response.Code)
	}
	request(t, handler, http.MethodPost, "/orders", map[string]any{
		"product_id": 1, "quantity": 2, "total_amount": 20,
		"discount_amount": 2, "start_time": "2026-10-02T10:00:00Z",
		"end_time": "2026-10-02T11:00:00Z", "payment_method": "cash",
	})
	if response := request(t, handler, http.MethodPatch, "/orders/1", map[string]string{
		"status": "confirmed",
	}); response.Code != http.StatusConflict {
		t.Fatalf("confirm defect status = %d", response.Code)
	}
}

func TestResetExistsOnlyInTestMode(t *testing.T) {
	data := newStore()
	production := newHandler(data, config{})
	if response := request(t, production, http.MethodPost, "/test/reset", nil); response.Code != http.StatusNotFound {
		t.Fatalf("production reset status = %d", response.Code)
	}

	testingHandler := newHandler(data, config{testMode: true})
	request(t, testingHandler, http.MethodPost, "/users", map[string]string{
		"name": "Ada", "email": "ada@example.test",
	})
	if response := request(t, testingHandler, http.MethodPost, "/test/reset", nil); response.Code != http.StatusNoContent {
		t.Fatalf("reset status = %d", response.Code)
	}
	if response := request(t, testingHandler, http.MethodGet, "/users/1", nil); response.Code != http.StatusNotFound {
		t.Fatalf("user survived reset: status=%d", response.Code)
	}
}
