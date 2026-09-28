"""Collection policy examples: original pytest outcomes must remain unchanged."""
import pytest
from selenium.webdriver.common.by import By

def test_pass(driver,local_site):
    driver.get(local_site);assert driver.title=='검색 실패 조사 예제'

def test_command_failure(driver,local_site):
    driver.get(local_site);driver.find_element(By.ID,'missing-element').click()

@pytest.mark.skip(reason='not run')
def test_skip(driver):pass

@pytest.mark.xfail(reason='known issue')
def test_xfail(driver,local_site):
    driver.get(local_site);assert False,'known issue'

@pytest.mark.xfail(strict=True,reason='unexpected success')
def test_strict_xpass(driver,local_site):
    driver.get(local_site);assert driver.title=='검색 실패 조사 예제'

@pytest.fixture
def teardown_error(driver,local_site):
    driver.get(local_site)
    yield driver
    raise RuntimeError('synthetic teardown error')

def test_teardown(teardown_error):assert teardown_error.title=='검색 실패 조사 예제'

@pytest.fixture
def setup_error():raise RuntimeError('synthetic failure before driver connection')

def test_setup(setup_error):pass

def test_unconnected_failure():assert False,'synthetic test without Selenium attachment'

def test_early_close(driver,local_site):
    driver.get(local_site);driver.quit();assert False,'synthetic assertion after driver close'
