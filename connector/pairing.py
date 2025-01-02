"""
Pairing and device management for G1 glasses
"""
import asyncio
from typing import Optional, Dict
from bleak import BleakScanner, BleakClient

class PairingManager:
    """Handles device pairing and management"""
    
    def __init__(self, connector):
        self.connector = connector
        self.logger = connector.logger
        self._pairing_lock = asyncio.Lock()
        self._discovery_cache = {}
        self._last_scan = 0

    async def discover_glasses(self, timeout: float = 15.0) -> Dict[str, Dict]:
        """Scan for available G1 glasses"""
        try:
            async with self._pairing_lock:
                self.logger.info("Starting glasses discovery...")
                devices = await BleakScanner.discover(timeout=timeout)
                
                discovered = {}
                for device in devices:
                    if device.name:
                        if "_L_" in device.name:
                            discovered['left'] = {
                                'address': device.address,
                                'name': device.name,
                                'rssi': device.rssi
                            }
                            self.logger.info(f"Found left glass: {device.name}")
                        elif "_R_" in device.name:
                            discovered['right'] = {
                                'address': device.address,
                                'name': device.name,
                                'rssi': device.rssi
                            }
                            self.logger.info(f"Found right glass: {device.name}")
                
                self._discovery_cache = discovered
                self._last_scan = asyncio.get_event_loop().time()
                return discovered
                
        except Exception as e:
            self.logger.error(f"Discovery failed: {e}")
            return {}

    async def _attempt_pairing(self, client: BleakClient, glass_name: str, max_attempts: int = 3) -> bool:
        """Attempt to pair with a glass"""
        try:
            is_left = glass_name == "Left glass"
            address = client.address  # Store address since we'll recreate the client
            
            self.logger.debug(f"Starting first-time pairing for {glass_name}")
            self.connector.console.print(f"\n[yellow]Performing first-time pairing for {glass_name}...[/yellow]")

            for attempt in range(1, max_attempts + 1):
                try:
                    # Create client without any special parameters first - exactly like g1_connector.py
                    client = BleakClient(address)
                    
                    # Connect first
                    await client.connect(timeout=20.0)
                    if client.is_connected:
                        self.logger.debug("Connection established")
                        
                        # Simple pair() call - exactly like g1_connector.py
                        await client.pair()
                        self.logger.debug("Pairing successful")
                        
                        # Update config immediately after successful pair
                        if is_left:
                            self.connector.config.left_paired = True
                        else:
                            self.connector.config.right_paired = True
                        self.connector.config.save()
                        
                        # Disconnect to finalize pairing
                        await client.disconnect()
                        await asyncio.sleep(1)
                        
                        self.connector.console.print(f"[green]{glass_name} paired and connected![/green]")
                        
                        # After successful connection, check initial state
                        initial_state = await self.connector.state_manager.handle_state_change(
                            self.connector.uart_service.last_state,
                            side="left" if glass_name == "Left glass" else "right"
                        )
                        
                        if not initial_state:
                            self.logger.warning(f"Could not get initial state for {glass_name}")
                        
                        return True
                        
                except Exception as e:
                    self.logger.error(f"Connection attempt {attempt} failed: {e}")
                    if attempt < max_attempts:
                        self.connector.console.print("[yellow]Retrying connection...[/yellow]")
                        await asyncio.sleep(2)
                    continue
            
            return False
            
        except Exception as e:
            self.logger.error(f"Pairing attempt failed: {e}")
            return False

    async def _verify_windows_pairing(self, address: str) -> bool:
        """Verify device is paired in Windows"""
        try:
            # Use BleakScanner to get paired devices
            devices = await BleakScanner.discover(timeout=5.0)
            for device in devices:
                if device.address.lower() == address.lower():
                    # Check if device is paired using Bleak's internal API
                    if hasattr(device, '_device_info') and hasattr(device._device_info, 'pairing'):
                        return device._device_info.pairing.is_paired
                    return True  # Fallback if we can't check pairing status
            return False
        except Exception as e:
            self.logger.error(f"Error verifying Windows pairing: {e}")
            return False

    async def verify_pairing(self) -> bool:
        """Verify existing pairing is valid"""
        try:
            self.logger.debug("Verifying pairing...")
            
            # First check if we have addresses
            if not self.connector.config.left_address or not self.connector.config.right_address:
                self.logger.debug("No saved addresses found")
                return False
            
            # First verification
            for side, addr in [("left", self.connector.config.left_address), 
                              ("right", self.connector.config.right_address)]:
                try:
                    client = BleakClient(addr)
                    await client.connect(timeout=5.0)
                    await client.disconnect()
                    self.logger.debug(f"Successfully verified {side} glass pairing")
                except Exception as e:
                    self.logger.warning(f"Could not verify {side} glass pairing: {e}")
                    return False
            
            self.logger.info("Pairing verification successful")
            
            # If not paired, do first-time pairing
            if not self.connector.config.left_paired or not self.connector.config.right_paired:
                self.logger.info("\nFirst time connection detected!")
                self.logger.info("The glasses will be paired with your device. This only happens once.")
                self.logger.info("Please wait while the pairing is completed...")
                
                # Second verification
                self.logger.debug("Verifying pairing...")
                for side, addr in [("left", self.connector.config.left_address), 
                                 ("right", self.connector.config.right_address)]:
                    try:
                        client = BleakClient(addr)
                        await client.connect(timeout=5.0)
                        await client.pair()  # Add pairing here
                        await client.disconnect()
                        self.logger.debug(f"Successfully verified {side} glass pairing")
                        if side == "left":
                            self.connector.config.left_paired = True
                        else:
                            self.connector.config.right_paired = True
                    except Exception as e:
                        self.logger.warning(f"Could not verify {side} glass pairing: {e}")
                        return False
                
                self.connector.config.save()
                self.logger.info("Pairing verification successful")
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error verifying pairing: {e}")
            return False

    async def pair_glasses(self) -> bool:
        """Pair with discovered glasses"""
        try:
            # Check if we need a new scan
            if not self._discovery_cache or \
               asyncio.get_event_loop().time() - self._last_scan > 60:
                await self.discover_glasses()
            
            if 'left' not in self._discovery_cache or 'right' not in self._discovery_cache:
                self.logger.error("Could not find both glasses")
                return False
            
            # Update config with discovered devices
            self.connector.config.left_address = self._discovery_cache['left']['address']
            self.connector.config.right_address = self._discovery_cache['right']['address']
            self.connector.config.left_name = self._discovery_cache['left']['name']
            self.connector.config.right_name = self._discovery_cache['right']['name']
            
            # Create clients for pairing
            left_client = BleakClient(self.connector.config.left_address)
            right_client = BleakClient(self.connector.config.right_address)
            
            # Attempt pairing
            if not await self._attempt_pairing(left_client, "Left glass"):
                return False
            
            if not await self._attempt_pairing(right_client, "Right glass"):
                return False
            
            self.logger.info("Successfully paired with both glasses")
            return True
            
        except Exception as e:
            self.logger.error(f"Pairing failed: {e}")
            return False

    async def unpair_glasses(self) -> None:
        """Unpair from glasses"""
        try:
            self.connector.config.left_paired = False
            self.connector.config.right_paired = False
            self.connector.config.left_address = None
            self.connector.config.right_address = None
            self.connector.config.left_name = None
            self.connector.config.right_name = None
            
            self.logger.info("Unpaired from glasses")
            
        except Exception as e:
            self.logger.error(f"Error unpairing: {e}") 