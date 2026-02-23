import pytest
from core.order_manager import OrderStateMachine
from models.order import OrderStatus

def test_order_state_machine_valid_transitions():
    sm = OrderStateMachine()
    
    # DRAFT -> SIMULATED
    assert sm.can_transition(OrderStatus.DRAFT, OrderStatus.SIMULATED) == True
    
    # SIMULATED -> STAGED
    assert sm.can_transition(OrderStatus.SIMULATED, OrderStatus.STAGED) == True
    
    # STAGED -> SUBMITTED
    assert sm.can_transition(OrderStatus.STAGED, OrderStatus.SUBMITTED) == True
    
    # SUBMITTED -> PENDING
    assert sm.can_transition(OrderStatus.SUBMITTED, OrderStatus.PENDING) == True
    
    # PENDING -> PARTIAL_FILL
    assert sm.can_transition(OrderStatus.PENDING, OrderStatus.PARTIAL_FILL) == True
    
    # PARTIAL_FILL -> FILLED
    assert sm.can_transition(OrderStatus.PARTIAL_FILL, OrderStatus.FILLED) == True

def test_order_state_machine_invalid_transitions():
    sm = OrderStateMachine()
    
    # DRAFT -> FILLED (Invalid)
    assert sm.can_transition(OrderStatus.DRAFT, OrderStatus.FILLED) == False
    
    # FILLED -> PENDING (Invalid)
    assert sm.can_transition(OrderStatus.FILLED, OrderStatus.PENDING) == False
    
    # CANCELED -> SUBMITTED (Invalid)
    assert sm.can_transition(OrderStatus.CANCELED, OrderStatus.SUBMITTED) == False
