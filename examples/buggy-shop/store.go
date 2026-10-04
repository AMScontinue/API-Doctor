package main

import "sync"

type user struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
}

type order struct {
	ID             int     `json:"id"`
	ProductID      int     `json:"product_id"`
	Quantity       int     `json:"quantity"`
	TotalAmount    float64 `json:"total_amount"`
	DiscountAmount float64 `json:"discount_amount"`
	StartTime      string  `json:"start_time"`
	EndTime        string  `json:"end_time"`
	PaymentMethod  string  `json:"payment_method"`
	CardToken      string  `json:"card_token,omitempty"`
	Status         string  `json:"status"`
}

type store struct {
	mu          sync.RWMutex
	nextUserID  int
	nextOrderID int
	users       map[int]user
	orders      map[int]order
}

func newStore() *store {
	value := &store{}
	value.reset()
	return value
}

func (s *store) reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nextUserID = 1
	s.nextOrderID = 1
	s.users = make(map[int]user)
	s.orders = make(map[int]order)
}

func (s *store) createUser(value user) user {
	s.mu.Lock()
	defer s.mu.Unlock()
	value.ID = s.nextUserID
	s.nextUserID++
	s.users[value.ID] = value
	return value
}

func (s *store) getUser(id int) (user, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	value, ok := s.users[id]
	return value, ok
}

func (s *store) deleteUser(id int) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.users[id]; !ok {
		return false
	}
	delete(s.users, id)
	return true
}

func (s *store) createOrder(value order) order {
	s.mu.Lock()
	defer s.mu.Unlock()
	value.ID = s.nextOrderID
	s.nextOrderID++
	value.Status = "created"
	s.orders[value.ID] = value
	return value
}

func (s *store) getOrder(id int) (order, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	value, ok := s.orders[id]
	return value, ok
}
