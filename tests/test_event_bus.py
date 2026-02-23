import pytest
import asyncio
from core.event_bus import EventBus

@pytest.mark.asyncio
async def test_event_bus_pub_sub():
    # Use a test database or mock asyncpg
    # For this test, we'll just mock the asyncpg pool and connection
    class MockConnection:
        def __init__(self):
            self.listeners = {}
            
        async def add_listener(self, channel, callback):
            self.listeners[channel] = callback
            
        async def remove_listener(self, channel, callback):
            if channel in self.listeners:
                del self.listeners[channel]
                
        async def close(self):
            pass
            
        async def execute(self, query):
            # Parse NOTIFY channel, 'payload'
            if query.startswith("NOTIFY"):
                parts = query.split(" ", 2)
                channel = parts[1].strip(",")
                payload = parts[2].strip("'")
                if channel in self.listeners:
                    self.listeners[channel](self, 0, channel, payload)

    class MockPool:
        def __init__(self, conn):
            self.conn = conn
            
        class AcquireContext:
            def __init__(self, conn):
                self.conn = conn
            async def __aenter__(self):
                return self.conn
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
                
        def acquire(self):
            return self.AcquireContext(self.conn)
            
        async def close(self):
            pass

    bus = EventBus("mock_dsn")
    bus._conn = MockConnection()
    bus._pool = MockPool(bus._conn)
    bus._running = True
    
    received_data = []
    
    async def callback(data):
        received_data.append(data)
        
    await bus.subscribe("TEST_CHANNEL", callback)
    await bus.publish("TEST_CHANNEL", {"message": "hello"})
    
    # Wait for the async callback task to finish
    await asyncio.sleep(0.1)
    
    assert len(received_data) == 1
    assert received_data[0] == {"message": "hello"}
    
    await bus.stop()
